import argparse
import os
import sys
import time
from datetime import timedelta

import torch
from datasets import load_dataset
from datasets import load_metric
from torch.utils.data import DataLoader
from transformers import AutoModelForSequenceClassification
from transformers import AutoTokenizer

import torchdynamo

torchdynamo.config.fake_tensor_propagation = False

# You will download around 84G dataset if you run this end to end training/evaluation example.

os.environ["TOKENIZERS_PARALLELISM"] = "false"
device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")


def data_processing(num_samples, batch_size):
    dataset = load_dataset("yelp_review_full")
    tokenizer = AutoTokenizer.from_pretrained("bert-base-cased")

    def tokenize_function(examples):
        return tokenizer(examples["text"], padding="max_length", truncation=True)

    tokenized_datasets = dataset.map(tokenize_function, batched=True)

    tokenized_datasets = tokenized_datasets.remove_columns(["text"])
    tokenized_datasets = tokenized_datasets.rename_column("label", "labels")
    tokenized_datasets.set_format("torch")

    small_train_dataset = tokenized_datasets["train"].select(range(num_samples))
    small_eval_dataset = tokenized_datasets["test"].select(range(num_samples))

    train_dataloader = DataLoader(small_train_dataset, batch_size=batch_size)
    eval_dataloader = DataLoader(small_eval_dataset, batch_size=batch_size)

    return train_dataloader, eval_dataloader


def training_iter_fn(batch, model, optimizer):
    outputs = model(**batch)
    loss = outputs.loss
    loss.backward()
    optimizer.step()
    optimizer.zero_grad()
    return loss


def model_training_evaluation(
    backend, train_dataloader, eval_dataloader, model, optimizer, num_epochs
):
    model.to(device)
    model.train()
    loss_history = []
    if not backend:
        # Run with native Pytorch
        opt_training_iter_fn = training_iter_fn
    else:
        # Support backends: eager, aot_nop and aot_nvfuser
        opt_training_iter_fn = torchdynamo.optimize(backend)(training_iter_fn)
    for epoch in range(num_epochs):
        running_loss = 0.0
        for i, batch in enumerate(train_dataloader, 0):
            batch = {k: v.to(device) for k, v in batch.items()}
            loss = opt_training_iter_fn(batch, model, optimizer)
            running_loss += loss.item()
            if i % 100 == 99:
                loss_history.append(running_loss / 100)
                running_loss = 0.0

    metric = load_metric("accuracy")
    model.eval()
    if not backend:
        opt_model = model
    else:
        opt_model = torchdynamo.optimize(backend)(model)
    for batch in eval_dataloader:
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            outputs = opt_model(**batch)

        logits = outputs.logits
        predictions = torch.argmax(logits, dim=-1)
        metric.add_batch(predictions=predictions, references=batch["labels"])

    return loss_history, metric.compute()


def check_loss(ref_loss, res_loss):
    assert len(ref_loss) == len(res_loss)
    length = len(ref_loss)
    x = min(length, 10)
    if sum(res_loss[-x:]) / 10 <= sum(ref_loss[-x:]) / 10 + 1e-1:
        return True
    else:
        return False


def parse_args():
    parser = argparse.ArgumentParser(
        description="TorchDynamo end to end training/evaluation benchmark"
    )
    parser.add_argument(
        "--epochs", type=int, default=10, help="number of epochs to train (default: 10)"
    )
    parser.add_argument(
        "--num-samples",
        type=int,
        default=1000,
        help="number of samples to train/eval (default: 1000)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="input batch size for training (default: 8)",
    )
    parser.add_argument(
        "--lr", type=float, default=5e-5, help="learning rate (default: 5e-5)"
    )
    parser.add_argument(
        "--backend",
        choices=torchdynamo.list_backends(),
        default="eager",
        help="train/evaluate model with a given backend (default: eager)",
    )
    parser.add_argument(
        "--optimizer",
        default="SGD",
        help="train model using a given optimizer (default: SGD)",
    )
    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    train_dataloader, eval_dataloader = data_processing(
        args.num_samples, args.batch_size
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        "bert-base-cased", num_labels=5
    )
    optimizer_cls = getattr(sys.modules["torch.optim"], args.optimizer)
    optimizer = optimizer_cls(model.parameters(), lr=args.lr)
    native_start = time.time()
    ref_loss, _ = model_training_evaluation(
        None, train_dataloader, eval_dataloader, model, optimizer, args.epochs
    )
    native_end = time.time()
    res_loss, _ = model_training_evaluation(
        args.backend, train_dataloader, eval_dataloader, model, optimizer, args.epochs
    )
    dynamo_end = time.time()
    if check_loss(ref_loss, res_loss):
        print(
            "[PASSED] TorchDynamo end to end training loss is less than or equal to native PyTorch"
        )
    else:
        print(
            "[FAILED] TorchDynamo end to end training loss is greater than native Pytorch"
        )
    native_elapsed = native_end - native_start
    dynamo_elapsed = dynamo_end - native_end
    print(f"Train model on {args.epochs} epochs:")
    print(f"PyTorch spent {timedelta(seconds=native_elapsed)}")
    print(f"TorchDynamo spent {timedelta(seconds=dynamo_elapsed)}")


if __name__ == "__main__":
    main()
