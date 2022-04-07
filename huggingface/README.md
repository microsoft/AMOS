# AMOS (Huggingface)

This repository contains the Huggingface version of scripts for fine-tuning AMOS pretrained models on GLUE and SQuAD benchmarks. The scripts are based on the [Huggingface Transformers Library](https://github.com/huggingface/transformers).

Paper: [Pretraining Text Encoders with Adversarial Mixture of Training Signal Generators](https://openreview.net/forum?id=sX3XaHwotOg)

## Requirements

The scripts require Python 3.6+ and the required Python packages can be installed via pip (running in a virtual environment is recommended):
```
pip3 install -r requirements.txt
```
In addition, if you would like to utilize `fp16` training, you need to install [apex](https://github.com/NVIDIA/apex).

## Pretrained Models

We release one [AMOS pretrained model](https://huggingface.co/microsoft/amos), which corresponds to the `base++` model mentioned in the paper. You do not need to download it manually as it will be automatically downloaded upon running the training scripts.

## Usage
 ```python
>>> import torch
>>> from amos.modeling_amos import AMOSModel
>>> from amos.configuration_amos import AMOSConfig
>>> from amos.tokenization_amos import AMOSTokenizer

>>> config = AMOSConfig.from_pretrained("microsoft/amos")

>>> model = AMOSModel.from_pretrained("microsoft/amos", config=config)
>>> tokenizer = AMOSTokenizer.from_pretrained("microsoft/amos")

>>> inputs = tokenizer.encode("Hello world!")
>>> outputs = model(torch.tensor([inputs]))

 ```

## GLUE Fine-tuning

The [General Language Understanding Evaluation (GLUE)](https://gluebenchmark.com/) benchmark is a collection of sentence- or sentence-pair language understanding tasks for evaluating and analyzing natural language understanding systems. 

**Download GLUE Data**: You can download the [GLUE data](https://gluebenchmark.com/tasks) by running [this script](https://gist.github.com/W4ngatang/60c2bdb54d156a41194446737ce03e2e) and unpack it to some directory.

**Fine-Tuning**: You can run the [`run_glue.sh`](run_glue.sh) script for fine-tuning on each GLUE task. An example for using the script for fine-tuning on MNLI is shown below:
```
TASK=MNLI
GLUE_DATASET_PATH=/path/to/downloaded/glue_data
OUT_PATH=./glue_finetune/amos
BSZ=32
LR=1e-5
EPOCH=2
WARMUP=0.0625
SEED=1

export CUDA_VISIBLE_DEVICES=0
bash run_glue.sh $TASK $GLUE_DATASET_PATH $OUT_PATH $BSZ $LR $EPOCH $WARMUP $SEED
```

**Optimal Hyperparameters**: The fine-tuning hyperparameters leading to the best dev set performance in our experiments are shown below (please note that the results and optimal hyperparameters might slightly differ in your runs due to different computation environments):

* AMOS base++

|  | MNLI-m/mm | QQP | QNLI | SST-2 | CoLA | RTE | MRPC | STS-B |
| ------ | ------ | ------ | ------ | ------ | ------ | ------ | ------ | ------ |
| BSZ | 32/32 | 32 | 32 | 32 | 16 | 16 | 32 | 16 |
| LR | 1e-5/1e-5 | 2e-5 | 1e-5 | 1e-5 | 2e-5 | 2e-5 | 5e-5 | 3e-5 |
| EPOCH | 2/2 | 5 | 3 | 5 | 10 | 10 | 5 | 5 |
| WARMUP | 0.0625/0.0625 | 0.0625 | 0.0625 | 0.0625 | 0.1 | 0.0625 | 0.1 | 0.0625 |
| Result | 90.4/90.3 | 92.4 | 94.4 | 95.8 | 71.2 | 86.6 | 90.9 | 91.6 |

## SQuAD 2.0 Fine-tuning 
[Stanford Question Answering Dataset (SQuAD)](https://rajpurkar.github.io/SQuAD-explorer/) is a reading comprehension dataset, consisting of questions posed by crowdworkers on a set of Wikipedia articles, where the answer to every question is a segment of text, or span, from the corresponding reading passage, or the question might be unanswerable. 

The SQuAD 2.0 dataset will be automatically downloaded upon running the training script.

**Fine-Tuning**: You can run the [`run_squad.sh`](run_squad.sh) script for fine-tuning on SQuAD 2.0. An example for using the script is shown below:
```
SQUAD_DATASET_PATH=/path/to/squad2_data/
OUT_PATH=./squad2_finetune/amos
BSZ=32
LR=3e-5
EPOCH=3
WARMUP=0.0625
SEED=1

export CUDA_VISIBLE_DEVICES=0
bash run_squad.sh $SQUAD_DATASET_PATH $OUT_PATH $BSZ $LR $EPOCH $WARMUP $SEED
```

**Optimal Hyperparameters**: The fine-tuning hyperparameters leading to the best dev set performance in our experiments are shown below (please note that the results and optimal hyperparameters might slightly differ in your runs due to different computation environments):

* AMOS base++

|  | EM | F1 |
| ------ | ------ | ------ |
| BSZ | 16 | 16 |
| LR | 2e-5 | 2e-5 |
| EPOCH | 3 | 3 |
| WARMUP | 0.1 | 0.1 |
| Result | 84.2 | 87.1 |
