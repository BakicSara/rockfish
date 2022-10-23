# Rockfish

Rockfish is the deep learning based tool for detecting 5mC DNA base modifications.

## Requirements

* ONT Guppy >= 5 (sup model)
* Python >=3.9
* CUDA (for GPU inference)

## Installation

0. a) Create new environment (e.g. Conda):
   ```shell
   conda create --name rockfish python=3.9
   ```
   
   b) Activate the environment
   ```shell
   conda activate rockfish
   ```

1. Clone the repository
   ```shell
   git clone ... rockfish && cd rockfish
   ```

2. Run installation
   ```shell
   pip install --extra-index-url https://download.pytorch.org/whl/cu113 .
   ```
   Note: "cu113" installs PyTorch for CUDA 11.3. If you want to install PyTorch for other CUDA version, replace "cu113" with appropriate version (e.g. for CUDA 10.2 "cu102").

3. Download models
   Available models: ***base***, ***small*** (or both with ***all***)
   ```shell
   rockfish download -m {all, base, small}
   ```


## Inference

1. Guppy basecalling
   ```shell
   guppy_basecaller -i <fast5_folder> -r -s <save_folder> --config <config_file> --fast5_out --device <cpu_or_gpus>
   ```
   Note: ```--fast5_out``` will output fast5 files with fastq and move_table groups needed for inference. This parameter is mandatory.

2. Run inference
   ```shell
   rockfish inference -i <saved_fast5_files> --reference <reference_path> --model_path <model_path> -r -t <n_workers> -b <batch_size> -d <devices>
   ```
   * Number of workers ```-t``` sets number of processes for generating the data.
   * Batch size ```-b``` is an optional parameter with default value of $4096$. However, for some GPUS (like V100 or A100 with 32GB/42 GB VRAM), it's appropriate to set it to a higher value (e.g. $n_{gpu} \times 8192$ or $n_{gpu} \times 16384$ for base model).
   * Examples of device parameter ```-d```:
     * CPU: No parameter
     * 1 GPU: ```-d 0```
     * 2 GPUs: ```-d 0,1```
     * 2 GPUs (3rd and 4th GPU): ```-d 2,3```

## Models
| Model | Encoder layers | Decoder layers | Features | Feedforward | Guppy config              |
|-------|----------------|----------------|----------|-------------|---------------------------|
| Base  | 12             | 12             | 256      | 1024        | dna_r9.4.1_450bps_sup.cfg |
| Small | 6              | 6              | 128      | 1024        | dna_r9.4.1_450bps_sup.cfg |

## Example
Run example script on Nanopolish [data](https://nanopolish.readthedocs.io/en/latest/quickstart_call_methylation.html):
```shell
cd scripts
GUPPY_PATH=<path_to_basecaller> ./example.sh
```

Result of the inference is ***predictions.tsv*** file. It is tab-delimited text file with four fileds:
  1. Read-id
  2. Contig name
  3. Position in contig
  4. Logit 


## Acknowledgement

This work has been supported in part by Croatian Science Foundation under the project Single genome and metagenome assembly (IP-2018-01-5886), by Epigenomics and Epitranscriptomics Research seed grant from Genome Institute of Singapore (GIS), by Career Development Fund (C210812037) from A*STAR, and by the A*STAR Computational Resource Centre through the use of its high-performance computing facilities.
