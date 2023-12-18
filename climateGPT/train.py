import argparse
import math
import os
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Dict

import structlog
import torch

import wandb
from climateGPT.export import model_export
from climateGPT.iterate import TokenIterator
from climateGPT.model import ModelArgs, Transformer

log = structlog.get_logger()


# -----------------------------------------------------------------------------
# I/O
@dataclass
class EvalConfig:
    out_dir: Path = Path("out")
    eval_interval: int = 1000
    log_interval: int = 100
    eval_iters: int = 100
    always_save_checkpoint: bool = (
        False  # if True, always save a checkpoint after each eval
    )
    init_weights: str = "random"  # or "checkpoint"


# data
@dataclass
class BatchConfig:
    batch_size: int = (
        1  # if gradient_accumulation_steps > 1, this is the micro-batch size
    )
    gradient_accumulation_steps: int = 1  # used to simulate larger batch sizes
    num_workers: int = 0
    seed_offset: int = 0


@dataclass
class OptimizerConfig:
    # adamw optimizer
    learning_rate: float = 5e-4  # max learning rate
    max_iters: int = 100000  # total number of training iterations
    weight_decay: float = 1e-1
    beta1: float = 0.9
    beta2: float = 0.95
    grad_clip: float = 1.0  # clip gradients at this value, or disable if == 0.0
    # learning rate decay settings
    decay_lr: bool = True  # whether to decay the learning rate
    warmup_iters: int = 1000  # how many steps to warm up for


# system
@dataclass
class SystemConfig:
    device: str = "mps:0"  # 'cpu', 'cuda', "mps"
    dtype: str = "float16"  # float32|bfloat16|float16
    compile: bool = False  # use PyTorch 2.0 to compile the model to be faster


# logging
@dataclass
class WandbLog:
    wandb_log: bool = False
    wandb_project: str = "climateGPT"
    wandb_run_name: str = "run" + datetime.now().strftime("%Y_%m_%d_%H_%M_%S")


# -----------------------------------------------------------------------------
torch.backends.cuda.matmul.allow_tf32 = True  # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True  # allow tf32 on cudnn


# -----------------------------------------------------------------------------
config_keys = [
    k
    for k, v in globals().items()
    if not k.startswith("_") and isinstance(v, (int, float, bool, str))
]
config = {k: globals()[k] for k in config_keys}  # will be useful for logging


# -----------------------------------------------------------------------------
# helps estimate an arbitrarily accurate loss over either split using many batches
@torch.no_grad()
def estimate_loss(model, iter_batches, eval_iters: int) -> Dict[str, float]:
    out = {}
    model.eval()
    for split in ["train", "val"]:
        batch_iter = iter_batches(split=split)
        losses = torch.zeros(eval_iters)  # keep on CPU
        for k in range(eval_iters):
            X, Y = next(batch_iter)
            with ctx:
                _ = model(X, Y)
                loss = raw_model.last_loss
            losses[k] = loss.item()  # type: ignore
        out[split] = losses.mean()
    model.train()
    return out


# learning rate decay scheduler (cosine with warmup)
def get_lr(
    it: int, warmup_iters: int, lr_decay_iters: int, min_lr: float, learning_rate: float
) -> float:
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * it / warmup_iters
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)


# -----------------------------------------------------------------------------


if __name__ == "__main__":
    # parse all arguments
    parser = argparse.ArgumentParser(
        description="Train a Transformer model on climate data",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        help="Directory to save checkpoints and logs",
        default=EvalConfig.out_dir,
    )
    parser.add_argument(
        "--eval-interval",
        type=int,
        help="How often to evaluate the model",
        default=EvalConfig.eval_interval,
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        help="How often to log training progress",
        default=EvalConfig.log_interval,
    )
    parser.add_argument(
        "--eval-iters",
        type=int,
        help="How many batches to use for evaluation",
        default=EvalConfig.eval_iters,
    )
    parser.add_argument(
        "--always-save-checkpoint",
        type=bool,
        help="Whether to save a checkpoint after each evaluation",
        default=EvalConfig.always_save_checkpoint,
    )
    parser.add_argument(
        "--init-weights",
        type=str,
        help="How to initialize the model (random or checkpoint)",
        default=EvalConfig.init_weights,
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        help="Batch size",
        default=BatchConfig.batch_size,
    )
    parser.add_argument(
        "--gradient-accumulation-steps",
        type=int,
        help="Number of gradient accumulation steps",
        default=BatchConfig.gradient_accumulation_steps,
    )
    parser.add_argument(
        "--num-workers",
        type=int,
        help="Number of workers for the dataloader",
        default=BatchConfig.num_workers,
    )
    parser.add_argument(
        "--seed-offset",
        type=int,
        help="Seed offset for the dataloader",
        default=BatchConfig.seed_offset,
    )

    parser.add_argument(
        "--learning-rate",
        type=float,
        help="Learning rate",
        default=OptimizerConfig.learning_rate,
    )
    parser.add_argument(
        "--max-iters",
        type=int,
        help="Max number of iterations for training",
        default=OptimizerConfig.max_iters,
    )
    parser.add_argument(
        "--weight-decay",
        type=float,
        help="Weight decay",
        default=OptimizerConfig.weight_decay,
    )
    parser.add_argument(
        "--beta1",
        type=float,
        help="Beta1 for AdamW",
        default=OptimizerConfig.beta1,
    )
    parser.add_argument(
        "--beta2",
        type=float,
        help="Beta2 for AdamW",
        default=OptimizerConfig.beta2,
    )
    parser.add_argument(
        "--grad-clip",
        type=float,
        help="Gradient clip value",
        default=OptimizerConfig.grad_clip,
    )
    parser.add_argument(
        "--decay-lr",
        type=bool,
        help="Whether to decay the learning rate",
        default=OptimizerConfig.decay_lr,
    )
    parser.add_argument(
        "--warmup-iters",
        type=int,
        help="Number of warmup iterations",
        default=OptimizerConfig.warmup_iters,
    )

    parser.add_argument(
        "--device",
        type=str,
        help="Device to use for training (cpu, cuda, mps)",
        default=SystemConfig.device,
    )
    parser.add_argument(
        "--dtype",
        type=str,
        help="Data type to use for training (float32, bfloat16, float16)",
        default=SystemConfig.dtype,
    )

    parser.add_argument(
        "--compile",
        action="store_true",
        help="Enable pytorch model compiling (requires torch>=2.0",
        default=SystemConfig.compile,
    )

    parser.add_argument(
        "--wandb-log",
        action="store_true",
        help="Enable logging to wandb",
        default=WandbLog.wandb_log,
    )

    parser.add_argument(
        "--dim",
        type=int,
        help="Dimension of the model embeddings",
        default=ModelArgs.dim,
    )
    parser.add_argument(
        "--n-layers",
        type=int,
        help="Number of layers in the model (transformer blocks + FF)",
        default=ModelArgs.n_layers,
    )
    parser.add_argument(
        "--n-heads",
        type=int,
        help="Number of attention heads in the model",
        default=ModelArgs.n_heads,
    )
    parser.add_argument(
        "--vocab-size",
        type=int,
        help="Vocabulary size",
        default=ModelArgs.vocab_size,
    )
    parser.add_argument(
        "--hidden-dim",
        type=int,
        help="Dimension of hidden layer in the model",
        default=ModelArgs.hidden_dim,
    )
    parser.add_argument(
        "--hidden-dim-multiplier",
        type=int,
        help="Multiplier for the hidden layer dimension",
        default=ModelArgs.hidden_dim_multiplier,
    )
    parser.add_argument(
        "--multiple-of",
        type=int,
        help="MLP hidden layer size will be multiple of this value",
        default=ModelArgs.multiple_of,
    )
    parser.add_argument(
        "--norm-eps",
        type=float,
        help="Epsilon for layer normalization",
        default=ModelArgs.norm_eps,
    )
    parser.add_argument(
        "--max-context-length",
        type=int,
        help="Max context length",
        default=ModelArgs.max_context_length,
    )
    parser.add_argument(
        "--dropout",
        type=float,
        help="Dropout probability",
        default=ModelArgs.dropout,
    )

    args = parser.parse_args()

    # instantiate all parameters
    eval_config = EvalConfig(
        out_dir=args.out_dir,
        eval_interval=args.eval_interval,
        log_interval=args.log_interval,
        eval_iters=args.eval_iters,
        always_save_checkpoint=args.always_save_checkpoint,
        init_weights=args.init_weights,
    )
    model_config = ModelArgs(
        dim=args.dim,
        n_layers=args.n_layers,
        n_heads=args.n_heads,
        vocab_size=args.vocab_size,
        hidden_dim=args.hidden_dim,
        hidden_dim_multiplier=args.hidden_dim_multiplier,
        multiple_of=args.multiple_of,
        norm_eps=args.norm_eps,
        max_context_length=args.max_context_length,
        dropout=args.dropout,
    )
    batch_config = BatchConfig(
        batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        num_workers=args.num_workers,
        seed_offset=args.seed_offset,
    )
    optimizer_config = OptimizerConfig(
        learning_rate=args.learning_rate,
        max_iters=args.max_iters,
        weight_decay=args.weight_decay,
        beta1=args.beta1,
        beta2=args.beta2,
        grad_clip=args.grad_clip,
        decay_lr=args.decay_lr,
        warmup_iters=args.warmup_iters,
    )
    system_config = SystemConfig(
        device=args.device,
        dtype=args.dtype,
        compile=args.compile,
    )
    wandb_log = WandbLog(
        wandb_log=args.wandb_log,
    )

    ctx = (
        nullcontext()
        if system_config.device != "cuda"
        else torch.amp.autocast(
            device_type=system_config.device, dtype=system_config.dtype
        )
    )

    # -----------------------------------------------------------------------------
    # fixing some hyperparams to sensible defaults
    lr_decay_iters = optimizer_config.max_iters  # should be ~= max_iters per Chinchilla
    min_lr = 0.0  # minimum learning rate, should be ~= learning_rate/10 per Chinchilla

    # -----------------------------------------------------------------------------
    tokens_per_iter = (
        batch_config.gradient_accumulation_steps
        * batch_config.batch_size  # noqa
        * model_config.max_context_length  # noqa
    )
    log.info(f"tokens per iteration will be: {tokens_per_iter:,}")
    log.info(
        f"""breaks down as:
            > {batch_config.gradient_accumulation_steps} grad accum steps  *
            >  {batch_config.batch_size} batch size *
            >  {model_config.max_context_length} context length"""
    )
    eval_config.out_dir.mkdir(exist_ok=True)
    # -----------------------------------------------------------------------------
    torch.manual_seed(1337 + batch_config.seed_offset)

    # -----------------------------------------------------------------------------
    if eval_config.init_weights == "random":
        iter_num = 0
        best_val_loss = 1e9
        # model init
        log.info("Initializing a new model from scratch")
        model = Transformer(model_config)
    elif eval_config.init_weights == "checkpoint":
        log.info(f"Resuming training from {eval_config.out_dir}")
        # resume training from a checkpoint.
        ckpt_path = Path(eval_config.out_dir, "ckpt.pt")
        checkpoint = torch.load(ckpt_path, map_location=system_config.device)
        checkpoint_model_args = checkpoint["model_args"]
        # force these config attributes to be equal otherwise we can't even resume
        #  training the rest of the attributes (e.g. dropout) can stay as desired from
        # command line
        for k in [
            "dim",
            "n_layers",
            "n_heads",
            "vocab_size",
            "multiple_of",
            "hidden_dim",
            "hidden_dim_multiplier",
            "max_context_length",
        ]:
            setattr(model_config, k, getattr(checkpoint_model_args, k))
        # create the model
        model = Transformer(model_config)
        state_dict = checkpoint["model"]
        # fix the keys of the state dictionary :(
        # honestly no idea how checkpoints sometimes get this prefix, have to debug more
        unwanted_prefix = "_orig_mod."
        for k, v in list(state_dict.items()):
            if k.startswith(unwanted_prefix):
                state_dict[k[len(unwanted_prefix) :]] = state_dict.pop(k)
        model.load_state_dict(state_dict)
        iter_num = checkpoint["iter_num"]
        best_val_loss = checkpoint["best_val_loss"]

    model.to(system_config.device)

    # compile the model
    if system_config.compile:
        log.info("compiling the model... (takes a ~minute)")
        unoptimized_model = model
        model = torch.compile(model)  # requires PyTorch 2.0

    optimizer = model.configure_optimizer(
        optimizer_config.weight_decay,
        optimizer_config.learning_rate,
        (optimizer_config.beta1, optimizer_config.beta2),
        system_config.device,
    )

    # initialize a GradScaler. If enabled=False scaler is a no-op
    scaler = torch.cuda.amp.GradScaler(enabled=(system_config.dtype == "float16"))

    # -----------------------------------------------------------------------------
    # Weight & Biases logging
    # logging
    if wandb_log.wandb_log:
        wandb.init(
            project=wandb_log.wandb_project,
            name=wandb_log.wandb_run_name,
            config=config,
        )

    # -----------------------------------------------------------------------------
    # Dataloader
    iter_params = {
        "pretokenized_source": Path(f"climateGPT/data/tok{model_config.vocab_size}"),
        "context_length": model_config.max_context_length,
        "split": "train",
    }

    iter_batches = partial(
        TokenIterator.iter_batches,
        batch_size=batch_config.batch_size,
        device=system_config.device,
        num_workers=batch_config.num_workers,
        **iter_params,
    )

    # training
    train_batch_iter = iter_batches(split="train")
    X, Y = next(train_batch_iter)  # fetch the very first batch
    t0 = time.time()
    local_iter_num = 0  # number of iterations in the lifetime of this process
    raw_model = model
    running_mfu = -1.0

    # training loop
    while True:
        # determine and set the learning rate for this iteration
        lr = (
            get_lr(
                it=iter_num,
                warmup_iters=optimizer_config.warmup_iters,
                lr_decay_iters=lr_decay_iters,
                min_lr=min_lr,
                learning_rate=optimizer_config.learning_rate,
            )
            if optimizer_config.decay_lr
            else optimizer_config.learning_rate
        )
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr

        # evaluate the loss on train/val sets and write checkpoints
        if iter_num % eval_config.eval_interval == 0:
            losses = estimate_loss(
                model=model,
                iter_batches=iter_batches,
                eval_iters=eval_config.eval_iters,
            )
            log.info(
                f"Step {iter_num}",
                train_loss=f"{losses['train']:.4f}",
                val_loss=f"{losses['val']:.4f}",
            )
            try:
                wandb.log(
                    {
                        "iter": iter_num,
                        "tokens": iter_num * tokens_per_iter,
                        "loss/train": losses["train"],
                        "loss/val": losses["val"],
                        "lr": lr,
                        "mfu": running_mfu * 100,  # convert to percentage
                    },
                    step=iter_num,
                )
            except Exception as e:
                log.info(f"logging to wandb failed: {e}")
            if losses["val"] < best_val_loss or eval_config.always_save_checkpoint:
                best_val_loss = losses["val"]
                if iter_num > 0:
                    checkpoint = {
                        "model": raw_model.state_dict(),
                        "optimizer": optimizer.state_dict(),
                        "model_args": model_config,
                        "iter_num": iter_num,
                        "best_val_loss": best_val_loss,
                        "config": config,
                    }
                    log.info(f"saving checkpoint to {eval_config.out_dir}")
                    torch.save(checkpoint, os.path.join(eval_config.out_dir, "ckpt.pt"))
                    model_export(
                        raw_model,
                        os.path.join(eval_config.out_dir, "model.bin"),
                        version=0,
                    )

        # forward backward update, with optional gradient accumulation

        for micro_step in range(batch_config.gradient_accumulation_steps):
            with ctx:
                logits = model(X, Y)
                loss = raw_model.last_loss
                loss = loss / batch_config.gradient_accumulation_steps  # type: ignore
            X, Y = next(train_batch_iter)  # fetch the next batch asynchrounously
            loss.backward()  # type: ignore
        # clip the gradient
        if optimizer_config.grad_clip != 0.0:
            torch.nn.utils.clip_grad_norm_(
                model.parameters(), optimizer_config.grad_clip
            )
        # step the optimizer and scaler if training in fp16
        scaler.step(optimizer)
        scaler.update()
        # flush the gradients as soon as we can, no need for this memory anymore
        optimizer.zero_grad(set_to_none=True)

        # timing and logging
        t1 = time.time()
        dt = t1 - t0
        t0 = t1
        if iter_num % eval_config.log_interval == 0:
            lossf = (
                loss.item() * batch_config.gradient_accumulation_steps  # type: ignore
            )
            if local_iter_num >= 5:  # let the training loop settle a bit
                mfu = raw_model.estimate_mfu(
                    batch_config.batch_size * batch_config.gradient_accumulation_steps,
                    dt,
                    flops_promised=2.6e12,
                )
                running_mfu = (
                    mfu if running_mfu == -1.0 else 0.9 * running_mfu + 0.1 * mfu
                )
            log.info(
                f"Step {iter_num}",
                loss=f"{lossf:.4f}",
                lr=f"{lr:e}",
                ms=f"{dt*1000:.2f}",
                mfu=f"{running_mfu*100:.2f}%",
            )
        iter_num += 1
        local_iter_num += 1

        # termination conditions
        if iter_num > optimizer_config.max_iters:
            break


# TODO: add MoE layer and training loop
# TODO: create instruct dataset
# TODO: revamp code from FastGPT repo
