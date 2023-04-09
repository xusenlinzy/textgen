# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description:
"""
from typing import Any, Dict, List, Optional, Tuple, Union

import os
import sys
import re
import random
import math

import numpy as np
import jieba
from rouge_chinese import Rouge
from nltk.translate.bleu_score import sentence_bleu, SmoothingFunction
import torch
import torch.nn as nn
from loguru import logger
from peft import (
    get_peft_model,
    LoraConfig,
    TaskType,
    PeftModel,
    get_peft_model_state_dict,
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
)
from tqdm.auto import tqdm
import transformers
from transformers import Trainer, TrainingArguments, AutoTokenizer, AutoModel, AutoConfig
from transformers.trainer import TRAINING_ARGS_NAME
from textgen.config.model_args import ChatGlmArgs
from textgen.chatglm.chatglm_utils import load_hf_dataset, ChatGlmDataset

try:
    import wandb

    wandb_available = True
except ImportError:
    wandb_available = False

has_cuda = torch.cuda.is_available()
os.environ["TOKENIZERS_PARALLELISM"] = "FALSE"
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

MODEL_CLASSES = {
    "chatglm": (AutoConfig, AutoModel, AutoTokenizer),
}


class ChatGlmModel:
    def __init__(
            self,
            model_type,
            model_name,
            lora_name=None,
            args=None,
            use_cuda=has_cuda,
            cuda_device=-1,
            **kwargs,
    ):

        """
        Initializes a ChatGLMModel model.

        Args:
            model_type: The type of model (chatglm)
            model_name: The exact architecture and trained weights to use. This may be a Hugging Face Transformers compatible pre-trained model, a community model, or the path to a directory containing model files.
            lora_name (optional): Lora name
            args (optional): Default args will be used if this parameter is not provided. If provided, it should be a dict containing the args that should be changed in the default args.
            use_cuda (optional): Use GPU if available. Setting to False will force model to use CPU only.
            cuda_device (optional): Specific GPU that should be used. Will use the first available GPU by default.
            **kwargs (optional): For providing proxies, force_download, resume_download, cache_dir and other options specific to the 'from_pretrained' implementation where this will be supplied.
        """  # noqa: ignore flake8"
        model_type = model_type.lower()
        self.args = self._load_model_args(model_name)

        if isinstance(args, dict):
            self.args.update_from_dict(args)
        elif isinstance(args, ChatGlmArgs):
            self.args = args

        self.is_sweeping = False
        if self.args.manual_seed:
            random.seed(self.args.manual_seed)
            np.random.seed(self.args.manual_seed)
            torch.manual_seed(self.args.manual_seed)
            if self.args.n_gpu > 0:
                torch.cuda.manual_seed_all(self.args.manual_seed)

        if use_cuda:
            if torch.cuda.is_available():
                if cuda_device == -1:
                    self.device = torch.device("cuda")
                else:
                    self.device = torch.device(f"cuda:{cuda_device}")
            else:
                raise ValueError(
                    "'use_cuda' set to True when cuda is unavailable."
                    "Make sure CUDA is available or set `use_cuda=False`."
                )
        else:
            self.device = "cpu"
        logger.debug(f"Device: {self.device}")
        if not use_cuda:
            self.args.fp16 = False
            self.args.int8 = False

        self.results = {}
        config_class, model_class, tokenizer_class = MODEL_CLASSES[model_type]
        if model_name is None:
            model_name = "THUDM/chatglm-6b"
        config = AutoConfig.from_pretrained(model_name, trust_remote_code=True, **kwargs)
        if use_cuda and torch.cuda.is_available():
            self.model = model_class.from_pretrained(
                model_name, config=config, trust_remote_code=True, load_in_8bit=self.args.int8)
            if self.args.quantization_bit:
                logger.debug(f"Quantized to {self.args.quantization_bit} bit")
                self.model = self.model.quantize(self.args.quantization_bit)
            if self.args.fp16:
                self.model = self.model.half().cuda()
        else:
            self.model = model_class.from_pretrained(
                model_name, config=config, trust_remote_code=True).float()

        self.tokenizer_class = tokenizer_class
        if self.args.tokenizer_name:
            self.tokenizer = tokenizer_class.from_pretrained(self.args.tokenizer_name, trust_remote_code=True)
        else:
            self.tokenizer = tokenizer_class.from_pretrained(model_name, trust_remote_code=True)
            self.args.tokenizer_name = self.args.model_name

        self.args.model_type = model_type
        if model_name is None:
            self.args.model_name = "ChatGLM_from_scratch"
        else:
            self.args.model_name = model_name

        self.lora_name = lora_name
        self.lora_loaded = False

    def data_collator(self, batch):
        len_ids = [len(example) for example in batch]
        longest = max(len_ids)
        input_ids = []
        labels_list = []
        for ids_l, example in sorted(zip(len_ids, batch), key=lambda x: -x[0]):
            ids = list(example)
            seq_len = ids.index(self.tokenizer.bos_token_id) + 1  # is equal to prompt length
            ignore_idx = -100
            labels = ([ignore_idx] * (seq_len - 1) + ids[(seq_len - 1):] + [ignore_idx] * (longest - ids_l))
            ids = ids + [self.tokenizer.pad_token_id] * (longest - ids_l)
            _ids = torch.LongTensor(ids)
            labels_list.append(torch.LongTensor(labels))
            input_ids.append(_ids)
        input_ids = torch.stack(input_ids)
        labels = torch.stack(labels_list)
        return {"input_ids": input_ids, "labels": labels}

    def train_model(
            self,
            train_data,
            output_dir=None,
            args=None,
            eval_data=None,
            verbose=True,
            **kwargs,
    ):
        """
        Trains the model using 'train_data'

        Args:
            train_data: Pandas DataFrame containing the 3 columns - `instruction`, `input`, `output`.
                        - `instruction`: The instruction text. (E.g. `"correct the following:"`)
                        - `input`: The input text sequence. `instruction` is automatically prepended to form the full input. (<instruction> `\n` <input>)
                        - `output`: The target sequence
            output_dir: The directory where model files will be saved. If not given, self.args.output_dir will be used.
            args (optional): Optional changes to the args dict of the model. Any changes made will persist for the model.
            eval_data (optional): A DataFrame against which evaluation will be performed when evaluate_during_training is enabled. Is required if evaluate_during_training is enabled.
            verbose (optional): If True, all of the warnings related to data processing will be printed. 
            **kwargs: Additional metrics that should be used. Pass in the metrics as keyword arguments (name of metric: function to use).
                        A metric function should take in two parameters. The first parameter will be the true labels, and the second parameter will be the predictions. Both inputs
                        will be lists of strings. Note that this will slow down training significantly as the predicted sequences need to be generated.

        Returns:
            global_step: Number of global steps trained
            training_details: Average training loss if evaluate_during_training is False or full training progress scores if evaluate_during_training is True
        """  # noqa: ignore flake8"

        if args:
            self.args.update_from_dict(args)
        if self.args.evaluate_during_training and eval_data is None:
            raise ValueError(
                "evaluate_during_training is enabled but eval_data is not specified."
                " Pass eval_data to model.train_model() if using evaluate_during_training."
            )

        if not output_dir:
            output_dir = self.args.output_dir
        if (
                os.path.exists(output_dir)
                and os.listdir(output_dir)
                and not self.args.overwrite_output_dir
        ):
            raise ValueError(
                "Output directory ({}) already exists and is not empty."
                " Set args.overwrite_output_dir = True to overcome.".format(output_dir)
            )
        # update model train config
        self.model.gradient_checkpointing_enable()
        self.model.enable_input_require_grads()
        self.model.is_parallelizable = True
        self.model.model_parallel = True
        self.model.lm_head = CastOutputToFloat(self.model.lm_head)
        self.model.config.use_cache = False
        resume_from_checkpoint = self.args.resume_from_checkpoint

        # setup peft, add lora config
        if self.args.use_lora:
            peft_config = LoraConfig(
                task_type=TaskType.CAUSAL_LM,
                inference_mode=False,
                r=self.args.lora_rank,
                lora_alpha=self.args.lora_alpha,
                lora_dropout=self.args.lora_dropout,
                target_modules=self.args.lora_target_modules,
                bias=self.args.lora_bias,
            )
            if self.args.int8:
                self.model = prepare_model_for_int8_training(self.model)
            self.model = get_peft_model(self.model, peft_config)

            if resume_from_checkpoint:
                # Check the available weights and load them
                checkpoint_name = os.path.join(resume_from_checkpoint, "pytorch_model.bin")  # Full checkpoint
                if not os.path.exists(checkpoint_name):
                    checkpoint_name = os.path.join(
                        resume_from_checkpoint, "adapter_model.bin")  # only LoRA model - LoRA config above has to fit
                    resume_from_checkpoint = (
                        False  # So the trainer won't try loading its state
                    )
                # The two files above have a different name depending on how they were saved, but are actually the same.
                if os.path.exists(checkpoint_name):
                    logger.info(f"Restarting from {checkpoint_name}")
                    adapters_weights = torch.load(checkpoint_name)
                    self.model = set_peft_model_state_dict(self.model, adapters_weights)
                else:
                    logger.info(f"Checkpoint {checkpoint_name} not found")

            print_trainable_parameters(self.model)
            self.lora_loaded = True
        else:
            logger.error("only impl lora fine-tune, set `use_lora=True` for train.")
            raise ValueError("set `use_lora=True` for train.")
        self._move_model_to_device()
        os.makedirs(output_dir, exist_ok=True)

        # load dataset
        train_dataset = self.load_and_cache_examples(train_data)
        if verbose:
            logger.debug(f"train_dataset len: {len(train_dataset)}, train_dataset[0]: {train_dataset[0]}")
        eval_dataset = None
        if eval_data is not None:
            eval_dataset = self.load_and_cache_examples(eval_data, evaluate=True)
            if verbose:
                logger.debug(f"eval_dataset len: {len(eval_dataset)}, eval_dataset[0]: {eval_dataset[0]}")

        # start train
        training_args = TrainingArguments(
            output_dir=self.args.output_dir,
            auto_find_batch_size=True,
            learning_rate=self.args.learning_rate,
            num_train_epochs=self.args.num_train_epochs,
            logging_dir=f"{self.args.output_dir}/logs",
            logging_steps=self.args.logging_steps,
            max_steps=self.args.max_steps,
            per_device_train_batch_size=self.args.per_device_train_batch_size,
            per_device_eval_batch_size=self.args.per_device_train_batch_size,
            gradient_accumulation_steps=self.args.gradient_accumulation_steps,
            save_steps=self.args.save_steps,
            save_total_limit=self.args.save_total_limit,
            fp16=self.args.fp16,
            remove_unused_columns=self.args.remove_unused_columns,
            overwrite_output_dir=self.args.overwrite_output_dir,
            do_train=True,
            no_cuda=True if self.device == "cpu" else False,
            **kwargs
        )
        if training_args.should_log:
            # The default of training_args.log_level is passive, so we set log level at info here to have that default.
            transformers.utils.logging.set_verbosity_info()

        log_level = training_args.get_process_log_level()
        transformers.utils.logging.set_verbosity(log_level)
        transformers.utils.logging.enable_default_handler()
        transformers.utils.logging.enable_explicit_format()

        # Log on each process the small summary:
        logger.warning(
            f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
            + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
        )
        logger.info(f"Training/evaluation parameters {training_args}")

        trainer = FinetuneTrainer(
            model=self.model,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            args=training_args,
            tokenizer=self.tokenizer,
            data_collator=self.data_collator,
        )
        if self.args.only_lora_state_dict:
            old_state_dict = self.model.state_dict
            self.model.state_dict = (
                lambda self, *_, **__: get_peft_model_state_dict(
                    self, old_state_dict()
                )
            ).__get__(self.model, type(self.model))
        if torch.__version__ >= "2" and sys.platform != "win32":
            self.model = torch.compile(self.model)

        logger.info("*** Train ***")
        (global_step, training_loss, metrics) = trainer.train(resume_from_checkpoint=resume_from_checkpoint)
        self.handle_metrics("train", metrics, self.args.output_dir)
        self.results.update(metrics)
        self.save_model(model=self.model)

        if eval_data is not None:
            logger.info("*** Evaluate ***")
            if torch.cuda.is_available() and self.args.fp16:
                self.model = self.model.half().cuda()
            metrics = trainer.evaluate(
                metric_key_prefix="eval", do_sample=self.args.do_sample, top_p=self.args.top_p,
                max_length=self.args.max_length, temperature=self.args.temperature
            )
            logger.debug(f"eval metrics: {metrics}")
            self.handle_metrics("eval", metrics, self.args.output_dir)
            self.results.update(metrics)

        if verbose:
            logger.debug(f"metrics: {self.results}")
            logger.info(
                " Training of {} model complete. Saved to {}.".format(
                    self.args.model_name, output_dir
                )
            )
        return global_step, training_loss

    @staticmethod
    def handle_metrics(split, metrics, output_dir):
        """
        Log and save metrics

        Args:
        - split: one of train, val, test
        - metrics: metrics dict
        - output_dir: where to save the metrics
        """

        logger.info(f"***** {split} metrics *****")
        for key in sorted(metrics.keys()):
            logger.info(f"  {key} = {metrics[key]}")
        output_file = os.path.join(output_dir, f"{split}_results.txt")
        with open(output_file, "w") as writer:
            for key in sorted(metrics.keys()):
                writer.write("{} = {}\n".format(key, str(metrics[key])))

    def load_lora(self):
        if self.args.use_lora:
            if self.lora_name:
                self.model = PeftModel.from_pretrained(self.model, self.lora_name)
                logger.info(f"Loaded lora model from {self.lora_name}")
                self.lora_loaded = True
            else:
                lora_path = os.path.join(self.args.output_dir, self.args.lora_name)
                if lora_path and os.path.exists(lora_path):
                    self.model = PeftModel.from_pretrained(self.model, self.args.output_dir)
                    logger.info(f"Loaded lora model from {lora_path}")
                    self.lora_loaded = True
            if torch.__version__ >= "2" and sys.platform != "win32":
                self.model = torch.compile(self.model)

    def process_response(self, response):
        response = response.strip().replace("[[训练时间]]", "2023年")
        punkts = [
            [",", "，"],
            ["!", "！"],
            [":", "："],
            [";", "；"],
            ["\?", "？"],
        ]
        for item in punkts:
            response = re.sub(r"([\u4e00-\u9fff])%s" % item[0], r"\1%s" % item[1], response)
            response = re.sub(r"%s([\u4e00-\u9fff])" % item[0], r"%s\1" % item[1], response)
        return response

    @torch.no_grad()
    def predict(self, sentences, keep_prompt=False, max_length=None, **kwargs):
        """
        Performs predictions on a list of text.

        Args:
            sentences: A python list of text (str) to be sent to the model for prediction. 
            keep_prompt: Whether to keep the prompt in the generated text.
            max_length: The maximum length of the generated text.

        Returns:
            preds: A python list of the generated sequences.
        """  # noqa: ignore flake8"

        if not self.lora_loaded:
            self.load_lora()
        if torch.cuda.is_available() and self.args.fp16:
            self.model = self.model.half().cuda()
        self._move_model_to_device()
        self.model.eval()

        all_outputs = []
        # Batching
        for batch in tqdm(
                [
                    sentences[i: i + self.args.eval_batch_size]
                    for i in range(0, len(sentences), self.args.eval_batch_size)
                ],
                desc="Generating outputs",
                disable=self.args.silent,
        ):
            inputs = self.tokenizer(batch, padding=True, return_tensors='pt').to(self.device)
            gen_kwargs = {
                "max_length": max_length if max_length else self.args.max_length,
                "num_beams": self.args.num_beams,
                "do_sample": self.args.do_sample,
                "top_p": self.args.top_p,
                "temperature": self.args.temperature,
                "eos_token_id": self.tokenizer.eos_token_id,
                **kwargs
            }
            outputs = self.model.generate(**inputs, **gen_kwargs)
            for idx, (prompt_text, generated_sequence) in enumerate(zip(batch, outputs)):
                # Decode text
                text = self.tokenizer.decode(generated_sequence)
                prompt_len = len(prompt_text)
                gen_text = text[prompt_len:]
                gen_text = self.process_response(gen_text)
                if keep_prompt:
                    total_sequence = prompt_text + gen_text
                else:
                    total_sequence = gen_text
                all_outputs.append(total_sequence)
        return all_outputs

    @torch.no_grad()
    def chat(self, query: str, history: List[Tuple[str, str]] = None,
             keep_prompt=False, max_length=128, **kwargs):
        """
        Chat with the model
        :param query:
        :param history:
        :param keep_prompt:
        :param max_length:
        :param kwargs:
        :return: response, history
        """
        self._move_model_to_device()
        self.model.eval()
        if history is None:
            history = []
        if not history:
            prompt = query
        else:
            prompt = ""
            for i, (old_query, response) in enumerate(history):
                prompt += "[Round {}]\n问：{}\n答：{}\n".format(i, old_query, response)
            prompt += "[Round {}]\n问：{}\n答：".format(len(history), query)
        response = self.predict([prompt], keep_prompt=keep_prompt, max_length=len(prompt) + max_length, **kwargs)[0]
        history = history + [(query, response)]
        return response, history

    def _move_model_to_device(self):
        self.model.to(self.device)

    def load_and_cache_examples(
            self, data, evaluate=False, no_cache=False, verbose=True, silent=False
    ):
        """
        Creates a ChatGLMDataset from data.

        Utility function for train() and eval() methods. Not intended to be used directly.
        """

        tokenizer = self.tokenizer
        args = self.args

        if not no_cache:
            no_cache = args.no_cache

        if not no_cache:
            os.makedirs(self.args.cache_dir, exist_ok=True)

        mode = "dev" if evaluate else "train"

        if self.args.use_hf_datasets:
            dataset = load_hf_dataset(data, tokenizer, self.args, mode)
            return dataset
        elif args.dataset_class:
            CustomDataset = args.dataset_class
            return CustomDataset(tokenizer, args, data, mode)
        else:
            return ChatGlmDataset(
                tokenizer,
                self.args,
                data,
                mode,
            )

    def save_model(
            self, output_dir=None, optimizer=None, scheduler=None, model=None, results=None
    ):
        if not output_dir:
            output_dir = self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)

        if model and not self.args.no_save:
            # Take care of distributed/parallel training
            model_to_save = model.module if hasattr(model, "module") else model
            model_to_save.save_pretrained(output_dir)
            self.tokenizer.save_pretrained(output_dir)
            torch.save(self.args, os.path.join(output_dir, "training_args.bin"))
            if optimizer and scheduler and self.args.save_optimizer_and_scheduler:
                torch.save(
                    optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt")
                )
                torch.save(
                    scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt")
                )
            # save model
            self.save_model_args(output_dir)

    def save_model_args(self, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        self.args.save(output_dir)

    def _load_model_args(self, input_dir):
        args = ChatGlmArgs()
        args.load(input_dir)
        return args

    def get_named_parameters(self):
        return [n for n, p in self.model.named_parameters()]


class FinetuneTrainer(Trainer):
    def compute_loss(self, model, inputs, return_outputs=False):
        return model(
            input_ids=inputs["input_ids"],
            labels=inputs["labels"],
        ).loss

    def evaluate(
            self,
            eval_dataset=None,
            ignore_keys=None,
            metric_key_prefix: str = "eval",
            **gen_kwargs
    ):
        """
        Run evaluation and returns metrics.

        The calling script will be responsible for providing a method to compute metrics, as they are task-dependent
        (pass it to the init `compute_metrics` argument).

        You can also subclass and override this method to inject custom behavior.

        Args:
            eval_dataset (`Dataset`, *optional*):
                Pass a dataset if you wish to override `self.eval_dataset`. If it is an [`~datasets.Dataset`], columns
                not accepted by the `model.forward()` method are automatically removed. It must implement the `__len__`
                method.
            ignore_keys (`List[str]`, *optional*):
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions.
            metric_key_prefix (`str`, *optional*, defaults to `"eval"`):
                An optional prefix to be used as the metrics key prefix. For example the metrics "bleu" will be named
                "eval_bleu" if the prefix is `"eval"` (default)
            gen_kwargs:
                Additional `generate` specific kwargs.

        Returns:
            A dictionary containing the evaluation loss and the potential metrics computed from the predictions. The
            dictionary also contains the epoch number which comes from the training state.
        """

        gen_kwargs = gen_kwargs.copy()
        self._gen_kwargs = gen_kwargs
        return super().evaluate(eval_dataset, ignore_keys=ignore_keys, metric_key_prefix=metric_key_prefix)

    def prediction_step(
            self,
            model: nn.Module,
            inputs: Dict[str, Union[torch.Tensor, Any]],
            prediction_loss_only: bool,
            ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[float], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """
        Perform an evaluation step on `model` using `inputs`.

        Subclass and override to inject custom behavior.

        Args:
            model (`nn.Module`):
                The model to evaluate.
            inputs (`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.

                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument `labels`. Check your model's documentation for all accepted arguments.
            prediction_loss_only (`bool`):
                Whether or not to return the loss only.
            ignore_keys (`List[str]`, *optional*): Ignore the keys in the output of your model (if it is a dictionary)
        Return:
            Tuple[Optional[float], Optional[torch.Tensor], Optional[torch.Tensor]]: A tuple with the loss, logits and
            labels (each being optional).
        """

        if prediction_loss_only:
            return super().prediction_step(
                model, inputs, prediction_loss_only=prediction_loss_only, ignore_keys=ignore_keys
            )

        has_labels = "labels" in inputs
        inputs = self._prepare_inputs(inputs)

        # XXX: adapt synced_gpus for fairscale as well
        gen_kwargs = self._gen_kwargs.copy()
        if gen_kwargs.get("max_length") is None and gen_kwargs.get("max_new_tokens") is None:
            gen_kwargs["max_length"] = self.model.config.max_length
        gen_kwargs["num_beams"] = (
            gen_kwargs["num_beams"] if gen_kwargs.get("num_beams") is not None else self.model.config.num_beams
        )
        default_synced_gpus = False
        gen_kwargs["synced_gpus"] = (
            gen_kwargs["synced_gpus"] if gen_kwargs.get("synced_gpus") is not None else default_synced_gpus
        )

        if "attention_mask" in inputs:
            gen_kwargs["attention_mask"] = inputs.get("attention_mask", None)
        if "position_ids" in inputs:
            gen_kwargs["position_ids"] = inputs.get("position_ids", None)
        if "global_attention_mask" in inputs:
            gen_kwargs["global_attention_mask"] = inputs.get("global_attention_mask", None)

        # prepare generation inputs
        # some encoder-decoder models can have varying encoder's and thus
        # varying model input names
        if hasattr(self.model, "encoder") and self.model.encoder.main_input_name != self.model.main_input_name:
            generation_inputs = inputs[self.model.encoder.main_input_name]
        else:
            generation_inputs = inputs[self.model.main_input_name]

        gen_kwargs["input_ids"] = generation_inputs
        generated_tokens = self.model.generate(**gen_kwargs)
        generated_tokens = generated_tokens[:, generation_inputs.size()[-1]:]

        # in case the batch is shorter than max length, the output should be padded
        if gen_kwargs.get("max_length") is not None and generated_tokens.shape[-1] < gen_kwargs["max_length"]:
            generated_tokens = self._pad_tensors_to_max_len(generated_tokens, gen_kwargs["max_length"])
        elif gen_kwargs.get("max_new_tokens") is not None and generated_tokens.shape[-1] < (
                gen_kwargs["max_new_tokens"] + 1
        ):
            generated_tokens = self._pad_tensors_to_max_len(generated_tokens, gen_kwargs["max_new_tokens"] + 1)

        loss = None

        if self.args.prediction_loss_only:
            return loss, None, None

        if has_labels:
            labels = inputs["labels"]
            if gen_kwargs.get("max_length") is not None and labels.shape[-1] < gen_kwargs["max_length"]:
                labels = self._pad_tensors_to_max_len(labels, gen_kwargs["max_length"])
            elif gen_kwargs.get("max_new_tokens") is not None and labels.shape[-1] < (
                    gen_kwargs["max_new_tokens"] + 1
            ):
                labels = self._pad_tensors_to_max_len(labels, (gen_kwargs["max_new_tokens"] + 1))
        else:
            labels = None

        return loss, generated_tokens, labels

    def _pad_tensors_to_max_len(self, tensor, max_length):
        if self.tokenizer is not None and hasattr(self.tokenizer, "pad_token_id"):
            # If PAD token is not defined at least EOS token has to be defined
            pad_token_id = (
                self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.tokenizer.eos_token_id
            )
        else:
            if self.model.config.pad_token_id is not None:
                pad_token_id = self.model.config.pad_token_id
            else:
                raise ValueError("Pad_token_id must be set in the configuration of the model, in order to pad tensors")

        padded_tensor = pad_token_id * torch.ones(
            (tensor.shape[0], max_length), dtype=tensor.dtype, device=tensor.device
        )
        padded_tensor[:, : tensor.shape[-1]] = tensor
        return padded_tensor

    def compute_metrics(self, eval_preds):
        metrics = super().compute_metrics(eval_preds)
        metrics['loss'] = eval_preds.loss.mean().item()
        preds, labels = eval_preds
        if isinstance(preds, tuple):
            preds = preds[0]
        decoded_preds = self.tokenizer.batch_decode(preds, skip_special_tokens=True)
        decoded_labels = self.tokenizer.batch_decode(labels, skip_special_tokens=True)

        try:
            perplexity = math.exp(metrics["loss"])
        except Exception:
            perplexity = float("inf")
        metrics["perplexity"] = perplexity

        score_dict = {
            "rouge-1": [],
            "rouge-2": [],
            "rouge-l": [],
            "bleu-4": [],
        }
        for pred, label in zip(decoded_preds, decoded_labels):
            hypothesis = list(jieba.cut(pred))
            reference = list(jieba.cut(label))
            rouge = Rouge()
            scores = rouge.get_scores(' '.join(hypothesis), ' '.join(reference))
            result = scores[0]

            for k, v in result.items():
                score_dict[k].append(round(v["f"] * 100, 4))
            bleu_score = sentence_bleu([list(label)], list(pred), smoothing_function=SmoothingFunction().method3)
            score_dict["bleu-4"].append(round(bleu_score * 100, 4))

        for k, v in score_dict.items():
            score_dict[k] = float(np.mean(v))
        metrics.update(score_dict)
        return metrics

    def save_model(self, output_dir=None, _internal_call=False, lora_name='adapter_model.bin'):
        os.makedirs(output_dir, exist_ok=True)
        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))
        saved_params = {
            k: v.to("cpu") for k, v in self.model.named_parameters() if v.requires_grad
        }
        torch.save(saved_params, os.path.join(output_dir, lora_name))


class CastOutputToFloat(nn.Sequential):
    def forward(self, x):
        return super().forward(x).to(torch.float32)


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    logger.debug(
        f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}"
    )