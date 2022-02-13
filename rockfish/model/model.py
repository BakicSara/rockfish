import torch
import torch.nn as nn
import torch.nn.functional as F
from torchmetrics import Accuracy, AveragePrecision

import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.utilities.cli import LightningCLI, LightningArgumentParser
from datasets import RFDataModule
from layers import PositionalEncoding, RockfishEncoder, SignalPositionalEncoding

from typing import *

MASK_CLS_LABEL = 4


class Rockfish(pl.LightningModule):
    def __init__(self,
                 features: int = 384,
                 bases_len: int = 31,
                 nhead: int = 6,
                 dim_ff: int = 1536,
                 n_layers: int = 12,
                 pos_dropout: float = 0.1,
                 attn_dropout: float = 0.1,
                 lr: float = 3e-4,
                 wd: float = 0.0001,
                 signal_mask_prob: float = 0.1,
                 codebook_size: int = 64,
                 bases_mask_prob: float = 0.15,
                 bases_rand_mask_prob: float = 0.10,
                 block_size: int = 5) -> None:
        super(Rockfish, self).__init__()
        self.save_hyperparameters()

        self.central_base = bases_len // 2
        self.signal_mask_prob = signal_mask_prob
        self.bases_mask_prob = bases_mask_prob
        self.bases_rand_mask_prob = bases_rand_mask_prob
        self.block_size = block_size

        self.signal_embedding = nn.Linear(5, features)
        self.codebook = nn.Linear(features, codebook_size, bias=False)
        self.signal_pe = SignalPositionalEncoding(features)
        # self.signal_dropout = nn.Dropout(pos_dropout)

        # self.embedding_dropout = nn.Dropout(p=pos_dropout)
        self.ref_embedding = nn.Embedding(5, features)  # removed max_norm=1
        self.ref_pe = PositionalEncoding(features, pos_dropout, bases_len)

        self.encoder = RockfishEncoder(features, nhead, dim_ff, n_layers,
                                       attn_dropout)
        self.layer_norm = nn.LayerNorm(features)

        self.fc_mod = nn.Linear(features, 1)
        self.fc_mask = nn.Linear(features, 4)

        self.train_mod_acc = Accuracy()
        self.train_mask_acc = Accuracy()
        self.train_signal_mask_acc = Accuracy()

        self.val_acc = Accuracy()
        self.val_ap = AveragePrecision()

    def create_padding_mask(self, num_blocks, blocks_len):
        repeats = torch.arange(0, blocks_len, device=num_blocks.device)  # S
        repeats = repeats.expand(num_blocks.size(0), -1)  # BxS

        return repeats >= num_blocks.unsqueeze(-1)

    def mask_signal(self, signal, num_blocks):
        code_logits, masks = [], []
        for i in range(signal.shape[0]):
            mask = torch.rand(num_blocks[i],
                              device=self.device) < self.signal_mask_prob
            masks.append(mask)

            c_logits = self.codebook(signal[i, :num_blocks[i]][mask])  # mxK
            code_logits.append(c_logits)
            signal[i, :num_blocks[i]][mask] = 0.  # self.signal_mask

        return torch.cat(code_logits, dim=0), masks

    def get_context_code_probs(self, signal, masks):
        code_logits = []
        for i, m in enumerate(masks):
            c_logits = self.codebook(signal[i, :len(m)][m])
            code_logits.append(c_logits)

        return torch.cat(code_logits, dim=0)

    def forward(self, signal, bases, r_pos_enc, q_pos_enc, num_blocks):
        B, S, _ = signal.shape

        signal = self.signal_embedding(signal)  # BxSxE
        signal = self.signal_pe(signal, r_pos_enc, q_pos_enc)
        # signal = self.embedding_dropout(signal)

        padding_mask = self.create_padding_mask(num_blocks, S)  # BxS_out

        bases = self.ref_embedding(bases)
        bases = self.ref_pe(bases)

        _, bases = self.encoder(signal, bases, padding_mask)

        x = self.layer_norm(bases[:, self.central_base])
        #bases = self.layer_norm(bases)  # BxTxE
        #x = bases[:, self.central_base]

        return self.fc_mod(x).squeeze(-1)  # BxE -> B

    def forward_train(self,
                      signal,
                      bases,
                      r_pos_enc,
                      q_pos_enc,
                      num_blocks,
                      bases_mask=None):
        B, S, _ = signal.shape

        signal = self.signal_embedding(signal)  # BxSxE
        signal_code_logits, masks = self.mask_signal(signal, num_blocks)
        signal = self.signal_pe(signal, r_pos_enc, q_pos_enc, masks)
        # signal = self.signal_dropout(signal)

        padding_mask = self.create_padding_mask(num_blocks, S)  # BxS_out

        bases = self.ref_embedding(bases)
        bases = self.ref_pe(bases)

        signal, bases = self.encoder(signal, bases, padding_mask)

        context_code_logits = self.get_context_code_probs(signal, masks)

        bases = self.layer_norm(bases)  # BxTxE
        x = bases[:, self.central_base]

        mod_logits = self.fc_mod(x).squeeze(-1)  # BxE -> B

        if bases_mask is None:
            mask_logits = None
        else:
            mask_logits = self.fc_mask(bases[bases_mask])

        return mod_logits, mask_logits, signal_code_logits, context_code_logits

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(),
                                      self.hparams.lr,
                                      weight_decay=self.hparams.wd,
                                      eps=1e-6)
        return optimizer

    def optimizer_zero_grad(self, epoch, batch_idx, optimizer, optimizer_idx):
        optimizer.zero_grad(set_to_none=True)

    def get_diversity_loss(self, signal_code_logits):
        probs = signal_code_logits.softmax(dim=-1)
        avg_probs = probs.mean(dim=0)  # K
        log_avg_probs = avg_probs.log()

        return F.kl_div(log_avg_probs,
                        torch.tensor([1 / self.hparams.codebook_size] *
                                     self.hparams.codebook_size,
                                     device=log_avg_probs.device),
                        reduction='batchmean')

    def bases_masking(self, bases):
        probs = torch.rand(*bases.shape, device=bases.device)
        mask = probs < self.bases_mask_prob
        rand_mask = probs < self.bases_rand_mask_prob

        target_bases = bases[mask].clone()
        bases[mask & ~rand_mask] = MASK_CLS_LABEL
        bases[rand_mask] = torch.randint(high=MASK_CLS_LABEL,
                                         size=bases[rand_mask].shape,
                                         device=bases.device)

        return bases, mask, target_bases

    def training_step(self, batch, batch_idx):
        signals, bases, r_pos_enc, q_pos_enc, num_blocks, labels = batch
        bases, bases_mask, target_bases = self.bases_masking(bases)

        mod_logits, mask_logits, signal_code_logits, context_code_logits = self.forward_train(
            signals,
            bases,
            r_pos_enc,
            q_pos_enc,
            num_blocks,
            bases_mask=bases_mask)

        signal_mask_targets = signal_code_logits.argmax(dim=-1)
        signal_mask_loss = F.cross_entropy(context_code_logits,
                                           signal_mask_targets)
        diversity_loss = self.get_diversity_loss(signal_code_logits)

        mod_loss = F.binary_cross_entropy_with_logits(mod_logits,
                                                      labels.float())
        mask_loss = F.cross_entropy(mask_logits, target_bases)
        loss = mod_loss + 0.1 * (mask_loss + signal_mask_loss) + diversity_loss

        self.log('train_signal_mask_loss', signal_mask_loss)
        self.log('train_diversity_loss', diversity_loss)
        self.log('train_mod_loss', mod_loss)
        self.log('train_mask_loss', mask_loss)
        self.log('train_loss', loss)

        self.log('train_mod_acc',
                 self.train_mod_acc(mod_logits, (labels > 0.5).int()))
        self.log('train_mask_acc',
                 self.train_mask_acc(mask_logits, target_bases))
        self.log(
            'train_signal_mask_acc',
            self.train_signal_mask_acc(context_code_logits,
                                       signal_mask_targets))

        return loss

    def validation_step(self, batch, batch_idx):
        signals, bases, r_pos_enc, q_pos_enc, num_blocks, labels = batch

        logits = self(signals, bases, r_pos_enc, q_pos_enc, num_blocks)
        loss = F.binary_cross_entropy_with_logits(logits, labels.float())

        self.log('val_loss', loss, prog_bar=True)
        self.log('val_acc',
                 self.val_acc(logits, (labels > 0.5).int()),
                 prog_bar=True)
        self.log('val_ap', self.val_ap(logits, labels))


def get_trainer_defaults() -> Dict[str, Any]:
    trainer_defaults = {}

    model_checkpoint = ModelCheckpoint(monitor='val_acc',
                                       save_top_k=3,
                                       mode='max')
    trainer_defaults['callbacks'] = [model_checkpoint]

    wandb = WandbLogger(project='dna-mod', log_model=True, save_dir='wandb')
    trainer_defaults['logger'] = wandb

    return trainer_defaults


class RockfishLightningCLI(LightningCLI):
    def add_arguments_to_parser(self, parser: LightningArgumentParser) -> None:
        parser.link_arguments('model.bases_len', 'data.seq_len')
        parser.link_arguments('model.block_size', 'data.block_size')


def cli_main():
    RockfishLightningCLI(
        Rockfish,
        RFDataModule,
        seed_everything_default=42,  # 42 for first training, 43 self-distilation
        save_config_overwrite=True,
        trainer_defaults=get_trainer_defaults())


if __name__ == '__main__':
    cli_main()
