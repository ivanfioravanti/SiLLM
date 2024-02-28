import pathlib
import time
import logging
import math
import re

import tqdm
import numpy as np

import mlx.core as mx
import mlx.nn as nn
import mlx.optimizers as optim
from mlx.utils import tree_flatten, tree_unflatten

from sillm.llm import LLM
from sillm.args import ModelArgs
from sillm.training.dataset import Dataset

########
# Based on mlx-examples:
# https://github.com/ml-explore/mlx-examples/blob/e74889d0fa0fb49d95bfdf6a1dcad907713eb50e/lora/models.py#L55
# https://github.com/ml-explore/mlx-examples/blob/854ad8747a9c703773adf8866602b114f68aa54a/llms/mlx_lm/tuner/lora.py#L7
########
class LoRALinear(nn.Module):
    """
    Linear layer with LoRA weights.
    """
    @staticmethod
    def from_linear(
        linear: nn.Linear,
        rank: int = 8,
        alpha: float = 16,
        dropout: float = 0.05,
        scale : float = 10.0
        ):
        """
        Convert linear layer to LoRA linear layer.
        Args:
            linear: Linear layer to convert.
            rank: Rank to use for LoRA.
            alpha: Alpha to use for LoRA.
            dropout: Dropout to use for LoRA.
            scale: Scale to use for LoRA.
        Returns:
            LoRA linear layer.
        """
        output_dims, input_dims = linear.weight.shape
        if isinstance(linear, nn.QuantizedLinear):
            input_dims *= 32 // linear.bits
        bias = "bias" in linear

        lora_lin = LoRALinear(input_dims, output_dims, rank, alpha, dropout, scale, bias)
        lora_lin.linear = linear
        lora_lin.name = linear.name

        return lora_lin

    def __init__(self,
                 input_dims: int,
                 output_dims: int,
                 rank: int = 8,
                 alpha: float = 16,
                 dropout: float = 0.05,
                 scale : float = 10.0,
                 bias: bool = False
                 ):
        """
        Args:
            input_dims: Input dimensions.
            output_dims: Output dimensions.
            rank: Rank to use for LoRA.
            alpha: Alpha to use for LoRA.
            dropout: Dropout to use for LoRA.
            scale: Scale to use for LoRA.
            bias: Whether to use bias.
        """
        super().__init__()

        # Initialize linear layer weights
        self.linear = nn.Linear(input_dims, output_dims, bias=bias)

        # Initialize LoRA dropout
        self.lora_dropout = nn.Dropout(p=dropout)

        # Initialize LoRA weights
        self.scale = scale * (alpha / rank)
        bound = 1 / math.sqrt(input_dims)
        input_shape = (input_dims, rank)
        output_shape = (rank, output_dims)
        self.lora_a = mx.random.uniform(low=-bound, high=bound, shape=input_shape)
        self.lora_b = mx.zeros(shape=output_shape)

    @property
    def lora_size(self):
        """
        Returns:
            Number of LoRA parameters.
        """
        return self.lora_a.size + self.lora_b.size

    def __call__(self, x):
        """
        Args:
            x: Input tensor.
        Returns:
            Output tensor.
        """
        # Determine dtype
        dtype = self.linear.weight.dtype
        if isinstance(self.linear, nn.QuantizedLinear):
            dtype = self.linear.scales.dtype

        # Apply linear layer and LoRA
        y = self.linear(x.astype(dtype))
        z = (self.lora_dropout(x) @ self.lora_a) @ self.lora_b

        return y + self.scale * z
    
    def merge(self):
        """
        Merge LoRA weights into linear weights.
        Returns:
            Linear layer with merged weights.
        """
        linear = self.linear
        weight = linear.weight
        dtype = linear.weight.dtype

        quantized = isinstance(linear, nn.QuantizedLinear)
        if quantized:
            dtype = mx.float16
            group_size = linear.group_size
            bits = linear.bits
            weight = mx.dequantize(weight, linear.scales, linear.biases, group_size, bits)

        # Merge LoRA weights into linear weights
        update = (self.lora_a @ self.lora_b).transpose()
        weight = (weight + (self.scale * update)).astype(dtype)

        if quantized:
            output_dims, input_dims = weight.shape
            bias = "bias" in linear
            linear = nn.Linear(input_dims, output_dims, bias=bias)

            return nn.QuantizedLinear.from_linear(linear, group_size, bits)
        else:
            linear.weight = weight

            return linear

class TrainableLoRA(LLM):
    """
    Trainable LoRA model wrapper.
    """
    @staticmethod
    def from_model(llm: LLM):
        """
        Convert LLM to trainable LLM.
        Args:
            llm: LLM to convert.
        Returns:
            Trainable LLM.
        """
        model = TrainableLoRA(llm.model, llm.tokenizer, llm.args)
        model._quantization = llm._quantization

        return model
    
    def __init__(self,
                 model,
                 tokenizer,
                 args: ModelArgs
                 ):
        """
        Args:
            tokenizer: Tokenizer instance.
            args: Model arguments.
        """
        self.model = model
        self.tokenizer = tokenizer
        self.args = args

        self._lora = None
        self._lora_modules = []

    def init_lora(self,
                  num_layers: int = -1,
                  target_modules: str = "query_value",
                  rank: int = 8,
                  alpha: float = 16,
                  dropout: float = 0.05,
                  scale : float = 10.0
                  ):
        """
        Initialize LoRA for model.
        Args:
            num_layers: Number of layers to apply LoRA to.
            target_modules: Modules to apply LoRA to.
            rank: Rank to use for LoRA.
            alpha: Alpha to use for LoRA.
            dropout: Dropout to use for LoRA.
            scale: Scale to use for LoRA.
        """
        if self._lora is None:
            self.model.freeze()

            if num_layers < 0:
                num_layers = len(self.model.layers)

            self._lora = {
                "num_layers": num_layers,
                "target_modules": target_modules,
                "rank": rank
            }

            if target_modules == "all_linear":
                self._lora_modules = [
                    (key, LoRALinear.from_linear(module, rank=rank, alpha=alpha, dropout=dropout, scale=scale))
                    for key, module in self.model.named_modules()
                    if isinstance(module, nn.Linear) or isinstance(module, nn.QuantizedLinear)
                ]
            elif target_modules == "query_value":
                self._lora_modules = [
                    (key, LoRALinear.from_linear(module, rank=rank, alpha=alpha, dropout=dropout, scale=scale))
                    for key, module in self.model.named_modules()
                    if re.search(r"\.attention\.(wq|wv)$", key)
                ]
            if len(self._lora_modules) == 0:
                logging.error(f"No target modules found for LoRA: {target_modules}")
            self.model.update_modules(tree_unflatten(self._lora_modules))

            # Enable training mode
            self.model.train(mode=True)

            logging.info(f"Initialized LoRA with rank {rank} for {num_layers} layers")
            logging.debug(f"LoRA target modules: {target_modules}")
            logging.debug(f"LoRA parameters: Alpha {alpha}, Dropout {dropout}, Scale {scale}")

            trainable_params = 0
            for _, module in self._lora_modules:
                trainable_params += module.lora_size
            logging.debug(f"LoRA trainable parameters: {trainable_params/ 10**6:.2f}M")

    def merge_and_unload_lora(self):
        """
        Merge LoRA layers back into model.
        """
        if self._lora is not None:
            merged_modules = [
                (key, module.merge())
                for key, module in self._lora_modules
            ]
            self.model.update_modules(tree_unflatten(merged_modules))

            logging.info(f"Merged LoRA layers back into model")

        self._lora = None
        self._lora_modules = []

        # Disable training mode
        self.model.train(mode=False)

    def save_adapters(self,
                      adapter_path: str,
                      ):
        """
        Save adapter weights.
        Args:
            adapter_path: Path to save adapter weights to.
        """
        assert self._lora is not None

        state = dict(tree_flatten(self.model.trainable_parameters()))
        mx.save_safetensors(adapter_path, **state)

    def save_checkpoint(self,
                        checkpoint_path: str,
                        steps: int = -1
                        ):
        """
        Save model checkpoint.
        Args:
            checkpoint_path: Directory to save checkpoints to.
            steps: Number of steps.
        """
        assert self._lora is not None

        checkpoint_path = pathlib.Path(checkpoint_path)
        if steps >= 0:
            adapter_path = checkpoint_path / f"ckpt-{steps}.safetensors"
        else:
            adapter_path = checkpoint_path / f"ckpt-final.safetensors"

        state = dict(tree_flatten(self.model.trainable_parameters()))

        if adapter_path.suffix == ".safetensors":
            mx.save_safetensors(str(adapter_path), state)
        elif adapter_path.suffix == ".npz":
            mx.savez(str(adapter_path), **state)
        else:
            raise ValueError(f"Unknown file extension {adapter_path.suffix}")

        return str(adapter_path)

    def load_adapters(self,
                      adapter_path: str
                      ):
        """
        Load adapter weights.
        Args:
            adapter_path: Path to adapter weights.
        """
        assert pathlib.Path(adapter_path).exists(), adapter_path

        self.model.load_weights(adapter_path, strict=False)

        logging.info(f"Loaded adapter weights from {adapter_path}")

    ########
    # Based on mlx-examples:
    # https://github.com/ml-explore/mlx-examples/blob/e74889d0fa0fb49d95bfdf6a1dcad907713eb50e/lora/lora.py#L198
    ########
    def evaluate(self,
                 dataset: Dataset,
                 batch_size: int,
                 num_batches: int
                 ):
        """
        Evaluate model on dataset.
        Args:
            dataset: Dataset to evaluate on.
            batch_size: Batch size.
            num_batches: Number of batches to evaluate.
        Returns:
            Average loss.
        """
        all_losses = []
        num_tokens = 0
        for _, batch in zip(
            range(num_batches),
            dataset.iterate_batches(batch_size),
        ):
            losses, toks = self.loss(*batch)
            all_losses.append((losses * toks).item())
            num_tokens += toks.item()

        return np.sum(all_losses) / num_tokens
    
    def loss(self, *args, **kwargs):
        """
        Default loss function from model.
        """
        return self.model.loss(*args, **kwargs)

    ########
    # Based on mlx-examples:
    # https://github.com/ml-explore/mlx-examples/blob/e74889d0fa0fb49d95bfdf6a1dcad907713eb50e/lora/lora.py#L212
    ########
    def train(self, 
              dataset_training: Dataset,
              dataset_validation: Dataset,
              batch_size: int = 4,
              learning_rate: float = 1e-5,
              epochs: int = 1,
              iterations: int = 0,
              report_steps: int = 10,
              eval_steps: int = 100,
              eval_callback: callable = None,
              validation_samples: int = 40,
              debug: bool = False
              ):
        """
        Train model.
        Args:
            dataset_training: Training dataset.
            dataset_validation: Validation dataset.
            batch_size: Batch size.
            learning_rate: Learning rate.
            epochs: Number of epochs.
            iterations: Number of iterations.
            report_steps: Report every `report_steps` iterations.
            eval_steps: Evaluate every `eval_steps` iterations.
            eval_callback: Callback after eval.
            validation_batches: Number of batches to evaluate on divided by batch_size.
            debug: Whether to enable debug mode.
        """
        # Calculate number of iterations
        if iterations == 0:
            iterations = len(dataset_training) // batch_size
        
        # Calculate number of validation batches
        validation_batches = validation_samples // batch_size
        
        logging.info(f"Training the model for {epochs} epochs of {iterations} batch iterations with batch size {batch_size}")
        logging.debug(f"Training learning rate: {learning_rate}")
        
        optimizer = optim.Adam(learning_rate=learning_rate)

        # Create value and gradient function for loss
        loss_value_and_grad = nn.value_and_grad(self.model, self.loss)

        losses = []
        num_tokens = 0

        # Main training loop
        start = time.perf_counter()
        pbar_epochs = tqdm.tqdm(range(epochs), desc="Epoch")
        for epoch in pbar_epochs:
            pbar_iterations = tqdm.tqdm(range(iterations), desc="Iter.", leave=False)
            for iter in pbar_iterations:
                n = epoch * iterations + iter
                batch = next(dataset_training.iterate_batches(batch_size, train=True))

                # Forward and backward pass
                (loss_value, toks), grad = loss_value_and_grad(*batch)

                if debug and n > 0:
                    # Check for zero gradients
                    for module_name, module_grad in tree_flatten(grad):
                        if not mx.any(module_grad):
                            logging.debug(f"Gradient for module {module_name} is zero in iteration {n}")

                # Model update
                optimizer.update(self.model, grad)
                mx.eval(self.model.parameters(), optimizer.state, loss_value)

                # Record loss
                losses.append(loss_value.item())
                num_tokens += toks.item()

                # Report training loss if needed
                if (n + 1) % report_steps == 0:
                    train_loss = np.mean(losses)
                    stop = time.perf_counter()

                    pbar_epochs.write(f"#{n + 1}:\tTraining loss   {train_loss:.3f}\t{float(num_tokens) / (stop - start):.3f} tok/sec")
                    pbar_epochs.refresh()
                    
                    losses = []
                    num_tokens = 0
                    start = time.perf_counter()

                # Report validation loss if needed
                if n == 0 or (n + 1) % eval_steps == 0:
                    stop = time.perf_counter()
                    val_loss = self.evaluate(dataset_validation, batch_size, validation_batches)
                    start = time.perf_counter()
                    pbar_epochs.write(f"#{n + 1}:\tValidation loss {val_loss:.3f}\t{(start - stop):.3f} sec")

                    # Eval callback
                    msg = eval_callback(n + 1, val_loss)
                    if msg:
                        pbar_epochs.write(f"#{n + 1}:\t" + msg)

                    start = time.perf_counter()