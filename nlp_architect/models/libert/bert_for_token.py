# ******************************************************************************
# Copyright 2019-2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ******************************************************************************
"""BERT-based model for token classification."""

# pylint: disable=no-member, not-callable, attribute-defined-outside-init, arguments-differ, missing-function-docstring
# pylint: disable=too-many-ancestors, too-many-instance-attributes, too-many-arguments
import os
from argparse import Namespace
from pathlib import Path
from collections import OrderedDict
from os.path import realpath
from torch.nn import CrossEntropyLoss
import numpy as np
import pytorch_lightning as pl
from pytorch_lightning.utilities import rank_zero_only
from pytorch_lightning import _logger as log
import torch
from torch.utils.data import DataLoader, TensorDataset
torch.multiprocessing.set_sharing_strategy('file_system')
from seqeval.metrics import (precision_score, recall_score, f1_score, accuracy_score,
                             performance_measure)
from transformers import (
    BertConfig,
    BertTokenizer,
    get_linear_schedule_with_warmup,
    AdamW
)
import path_patterns
from shared_bert_module import InhouseBertForABSA
import absa_utils
from libert_model import LiBertForToken, LiBertConfig
#from libert_model_14heads import LiBertForToken, LiBertConfig

LIBERT_DIR = Path(realpath(__file__)).parent

MODEL_CONFIG = {
    'bert': (InhouseBertForABSA, LiBertConfig, BertTokenizer),
    'libert': (LiBertForToken, LiBertConfig, BertTokenizer)
}

class BertForToken(pl.LightningModule):
    """Lightning module for BERT for token classification."""
    def __init__(self, hparams):
        """Initialize a model, tokenizer and config."""
        super().__init__()
        if isinstance(hparams, dict):
            hparams = Namespace(**hparams)

        # data_root = Path(__file__).parent.absolute() / 'data' / 'csv' / hparams.
        general_data_root = Path(__file__).parent.absolute() / "data" # .../libert/data
        data_root = hparams.csv_dir # dir of linguistically-enriched data, e.g. .../libert/data/csv/spacy 
        
        self.labels = absa_utils.get_labels(general_data_root / hparams.labels)
        num_labels = len(self.labels)
        hparams.data_dir = data_root / hparams.data_dir

        if not hparams.cache_dir:
            hparams.cache_dir = LIBERT_DIR / 'cache'
        if not hparams.output_dir:
            hparams.output_dir = LIBERT_DIR / 'models'

        self.model_type, self.config_type, self.tokenizer_type = MODEL_CONFIG[hparams.model_type]

        self.config = self.config_type \
            .from_pretrained(hparams.model_name_or_path,
                             **({"num_labels": num_labels} if num_labels is not None else {}))

        if hasattr(self.config, 'add_extra_args') and callable(self.config.add_extra_args):
            self.config.add_extra_args(hparams)

        os.environ["TOKENIZERS_PARALLELISM"] = "true"
        self.tokenizer = self.tokenizer_type \
            .from_pretrained(hparams.model_name_or_path, cache_dir=hparams.cache_dir)

        self.pad_token_label_id = CrossEntropyLoss().ignore_index
        self.step_count = 0
        # self.tfmr_ckpts = {}

        self.model = self.model_type.from_pretrained(
            hparams.model_name_or_path,
            from_tf=bool(".ckpt" in hparams.model_name_or_path),
            config=self.config,
            cache_dir=hparams.cache_dir)

        self.hparams = hparams
        self.sentence_metrics = None

    def forward(self, **inputs):
        return self.model(**inputs)

    def prepare_data(self):
        "Called to initialize data. Use the call to construct features"
        for mode in "train", "dev", "test":
            cached_features_file = self._feature_file(mode)
            if os.path.exists(cached_features_file) and not self.hparams.overwrite_cache:
                log.debug("Loading features from cached file %s", cached_features_file)
                features = torch.load(cached_features_file)
            else:
                log.debug("Creating features from dataset file at %s", self.hparams.data_dir)
                examples = absa_utils.read_examples_from_file(self.hparams.data_dir, mode)
                features = absa_utils.convert_examples_to_features(
                    examples,
                    self.labels,
                    self.hparams.max_seq_length,
                    self.tokenizer,
                    self.hparams
                )
                log.debug("Saving features into cached file %s", cached_features_file)
                torch.save(features, cached_features_file)

    def load_dataset(self, mode, batch_size):
        "Load datasets. Called after prepare data."
        cached_features_file = self._feature_file(mode)
        log.debug("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_attention_mask = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
        if features[0].token_type_ids is not None:
            all_token_type_ids = torch.tensor([f.token_type_ids for f in features],
                                              dtype=torch.long)
        else:
            all_token_type_ids = torch.tensor([0 for f in features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_ids for f in features], dtype=torch.long)

        tensors = [all_input_ids, all_attention_mask, all_token_type_ids, all_label_ids]

        #### Attach pattern-classification & matching-AT (i.e. auxiliary task) info 
        op2at_opinion_masks = torch.tensor([f.op2at_opinion_mask.numpy() for f in features], dtype=torch.long)
        op2at_patterns = torch.tensor([f.op2at_patterns.numpy() for f in features], dtype=torch.long)
        op2at_tgt_asp_indinces = torch.tensor([f.op2at_tgt_asp_index.numpy() for f in features], dtype=torch.long)

        tensors.extend([op2at_opinion_masks, op2at_patterns, op2at_tgt_asp_indinces])              

        if self.model_type is LiBertForToken:
            #### Attatch syntactic info ###
            dep_heads = torch.tensor([f.dep_heads for f in features], dtype=torch.float)
            tensors.append(dep_heads)
        
        shuffle = mode == 'train'
        return DataLoader(TensorDataset(*tensors), batch_size=batch_size, shuffle=shuffle,
                          num_workers=self.hparams.num_workers, pin_memory=True)

    @staticmethod
    def map_to_inputs(batch):
        # see order of each batch tensor in `load_dataset`
        inputs = {"input_ids": batch[0], "attention_mask": batch[1], "token_type_ids": batch[2],
                  "labels": batch[3],
                  # add here auxiliary task data
                  "opinion_mask": batch[4], "patterns": batch[5], "tgt_asp_indices": batch[6]}
        if len(batch) >= 8:
            inputs["parse"] = batch[7]

        return inputs

    def training_step(self, batch, _):
        "Compute loss and log."
        inputs = self.map_to_inputs(batch)
        outputs = self(**inputs)
        loss = outputs["loss"]
        tensorboard_logs = {'train_loss_step': loss, 'lr': self.lr_scheduler.get_last_lr()[-1]}
         # Combine loss summed up from auxialry task classifiers
        if not self.config.baseline and "total_aux_loss" in outputs:  
            aux_loss = outputs["total_aux_loss"]
            tensorboard_logs.update(aux_train_loss_step=aux_loss)
            # add aux_loss to returned loss - this determines optimization!
            loss = loss + aux_loss
        return {'loss': loss, 'log': tensorboard_logs}

    def training_epoch_end(self, outputs):
        avg_loss = torch.stack([x['loss'] for x in outputs]).mean()
        tensorboard_logs = {'train_loss': avg_loss, 'step': self.current_epoch}
        return {'loss': avg_loss, 'log': tensorboard_logs}

    def validation_step(self, batch, _):
        "Compute validation."
        inputs = self.map_to_inputs(batch)
        outputs = self(**inputs)
        tmp_eval_loss, logits = outputs["loss"], outputs["logits"]
        preds = logits.detach().cpu()#.numpy()
        target = inputs["labels"].detach().cpu()#.numpy()
        ret = {"val_loss_step": tmp_eval_loss.detach().cpu(), "pred": preds, "target": target}
        # add input_ids to ret to allow outputing BIO predictions to file 
        ret["input_ids"] = inputs["input_ids"]

        if "patt_aux_logits" in outputs and "patterns" in inputs:
            patt_aux_logits = outputs["patt_aux_logits"]
            patt_aux_preds = patt_aux_logits.detach().cpu()#.numpy()
            patt_aux_labels = outputs["patt_aux_labels"].detach().cpu()#.numpy()
            ret.update(patt_aux_preds=patt_aux_preds, patt_aux_labels=patt_aux_labels, aux_op_locations=outputs["aux_op_locations"])
        if "asp_match_aux_logits" in outputs and "tgt_asp_indices" in inputs:
            asp_match_aux_logits = outputs["asp_match_aux_logits"]
            asp_match_aux_preds = asp_match_aux_logits.detach().cpu()#.numpy()
            asp_match_aux_labels = outputs["asp_match_gold_labels"].detach().cpu()#.numpy() # take gold labels from outputs, stacked per OTs corresponding to logits
            ret.update(asp_match_aux_preds=asp_match_aux_preds, asp_match_aux_labels=asp_match_aux_labels, aux_op_locations=outputs["aux_op_locations"])
        return ret

    def validation_epoch_end(self, outputs):
        eval_info, _, = self._eval_end(outputs)
        logs = eval_info["log"]
        logs['step'] = self.current_epoch
        return {"val_loss": logs["val_loss"], "log": logs}

    def test_step(self, batch, batch_nb):
        return self.validation_step(batch, batch_nb)

    def test_epoch_end(self, outputs):
        eval_info, prediction_info = self._eval_end(outputs)
        logs = eval_info["log"]
        logs['step'] = self.current_epoch
        
        self._write_predictions(**prediction_info)
        # `val_loss` is the key returned by `self._eval_end()` but actually refers to `test_loss`
        return {"test_loss": logs["val_loss"], "log": logs}

    def _write_predictions(self, predictions, gold_labels, inp_words, 
                           asp_match_predictions=None, asp_match_gold=None,
                           patt_predictions=None, patt_gold=None):
        out_fn = self.logger.save_dir / self.logger._name / f"epoch-{self.current_epoch}-predictions.txt"
        # if auxilary prediction data doesn't exists, prepare its data-structures with s
        with open(out_fn, "w", encoding="utf-8") as fout:
            for si, (s_preds, s_golds, s_words) in enumerate(zip(predictions, gold_labels, inp_words)): # iterate over sentences
                assert len(s_words) == len(s_preds) == len(s_golds)
                for wi, (word, pred_lbl, gold_lbl) in enumerate(zip(s_words, s_preds, s_golds)):
                    line = f"{word}\t{pred_lbl}\t{gold_lbl}"
                    # columns 4/5 are for AT-match prediction/gold
                    if asp_match_predictions and asp_match_gold:
                        line += f"\t{asp_match_predictions[si][wi]}\t{asp_match_gold[si][wi]}"
                    else:
                        line += "\tO\tO"
                    # columns 6/7 are for pattern prediction/gold
                    if patt_predictions and patt_gold:
                        line += f"\t{patt_predictions[si][wi]}\t{patt_gold[si][wi]}"
                    else:
                        line += "\tO\tO"
                    fout.write(line+"\n")
                fout.write("\n")  
    
        
    def _eval_end(self, outputs):
        "Evaluation called for both Val and Test"
        prediction_info = {}
        aux_results = {}
        # compute & log performance also for auxiliary tasks
        asp_match_predictions, pattern_predictions = None, None
        if "matched-AT" in self.config.auxiliary_tasks:
            asp_match_results, asp_match_predictions = path_patterns.aux_task_eval(outputs, aux_task="asp_match")
            prediction_info.update(asp_match_predictions)
            aux_results.update(asp_match_results)
        if "pattern" in self.config.auxiliary_tasks:
            pattern_results, pattern_predictions = path_patterns.aux_task_eval(outputs, aux_task="patt")
            prediction_info.update(pattern_predictions)
            aux_results.update(pattern_results)  
            

        val_loss_mean = torch.stack([x["val_loss_step"] for x in outputs]).mean()
        preds = np.concatenate([x["pred"] for x in outputs], axis=0)
        preds = np.argmax(preds, axis=-1)
        out_label_ids = np.concatenate([x["target"] for x in outputs], axis=0)
        label_map = dict(enumerate(self.labels))
        
        def refactor_seq_to_sentence_length(full_seq_batch, translation_func):
            ret = [[] for _ in range(full_seq_batch.shape[0])]
            for i in range(full_seq_batch.shape[0]):
                for j in range(full_seq_batch.shape[1]):
                    if out_label_ids[i, j] != self.pad_token_label_id:  # skips [CLS], [SEP], subtokens, and sequence padding  
                        ret[i].append(translation_func(full_seq_batch[i][j]))
            return ret
            
        target = refactor_seq_to_sentence_length(out_label_ids, label_map.get)
        pred = refactor_seq_to_sentence_length(preds, label_map.get)
        # apply the same refactoring for aux task predictions and gold, for preparing the prediction output file
        if "matched-AT" in self.config.auxiliary_tasks:
            
            prediction_info["asp_match_predictions"] = refactor_seq_to_sentence_length(
                np.concatenate(prediction_info["asp_match_predictions"], axis=0), lambda x:'O' if x==-1 else x)
            prediction_info["asp_match_gold"] = refactor_seq_to_sentence_length(
                np.concatenate(prediction_info["asp_match_gold"],axis=0), lambda x:'O' if x==-1 else x)
        if "pattern" in self.config.auxiliary_tasks:
            patt_label_map = dict(enumerate(self.hparams.all_patterns))
            patt_label_map[-1] = 'O'
            prediction_info["patt_predictions"] = refactor_seq_to_sentence_length(
                np.concatenate(prediction_info["patt_predictions"] ,axis=0), patt_label_map.get)
            prediction_info["patt_gold"] = refactor_seq_to_sentence_length(
                np.concatenate(prediction_info["patt_gold"], axis=0), patt_label_map.get)
        
        input_ids = np.concatenate([x["input_ids"].cpu() for x in outputs], axis=0)
        inp_words = [[] for _ in range(out_label_ids.shape[0])]
        for i in range(out_label_ids.shape[0]):
            for j in range(out_label_ids.shape[1]):
                if out_label_ids[i, j] != self.pad_token_label_id:  # skips [CLS], [SEP], subtokens, and sequence padding  
                    word = self.tokenizer.convert_ids_to_tokens(int(input_ids[i][j]))
                    # complement word with its corresponding subtokens
                    next_j = j+1; next_subtok = self.tokenizer.convert_ids_to_tokens(int(input_ids[i][next_j]))
                    while out_label_ids[i, next_j] == self.pad_token_label_id and next_subtok != '[SEP]':
                        word += next_subtok.lstrip('##')
                        next_j += 1; next_subtok = self.tokenizer.convert_ids_to_tokens(int(input_ids[i][next_j]))
                    inp_words[i].append(word)

        calc = lambda f: torch.tensor(f(target, pred))
        results = OrderedDict({
            "val_loss": val_loss_mean,
            "micro_precision": calc(precision_score),
            "micro_recall": calc(recall_score),
            "micro_f1": calc(f1_score),
            "micro_accuracy": calc(accuracy_score)
        })
        confusion = performance_measure(target, pred)
        type_metrics, macro_avg = absa_utils.detailed_metrics(target, pred)
        results.update(type_metrics)
        results.update(macro_avg)
        results.update(confusion)

        per_sentence = lambda f: [f([t], [p]) for t, p in zip(target, pred)]
        self.sentence_metrics = {
            "precision": per_sentence(precision_score),
            "recall": per_sentence(recall_score),
            "f1": per_sentence(f1_score),
            "accuracy": per_sentence(accuracy_score)
        }
        # collect ABSA prediction info to returned dict
        prediction_info.update(predictions=pred, gold_labels=target, inp_words=inp_words)

        results.update(aux_results)
        ret = results.copy()
        ret["log"] = results
        return ret, prediction_info
    

    def configure_optimizers(self):
        "Prepare optimizer and schedule (linear warmup and decay)"
        model = self.model
        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if
                           not any(nd in n for nd in no_decay)],
                "weight_decay": self.hparams.weight_decay,
            },
            {
                "params": [p for n, p in model.named_parameters() if
                           any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            }
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=self.hparams.learning_rate,
                          eps=self.hparams.adam_epsilon)
        self.opt = optimizer
        return [optimizer]

    def optimizer_step(self, epoch, batch_idx, optimizer, optimizer_idx, second_order_closure=None,
                       on_tpu=False, using_native_amp=False, using_lbfgs=False):
        optimizer.step()
        optimizer.zero_grad()
        self.lr_scheduler.step()  # By default, PL will only step every epoch.

    def train_dataloader(self):
        train_batch_size = self.hparams.train_batch_size
        dataloader = self.load_dataset("train", train_batch_size)
        gpus = self.hparams.gpus
        num_gpus = len(gpus) if isinstance(gpus, list) else gpus
        t_total = (
            (len(dataloader.dataset) // (train_batch_size * max(1, num_gpus)))
            // self.hparams.accumulate_grad_batches
            * float(self.hparams.max_epochs)
        )
        self.lr_scheduler = get_linear_schedule_with_warmup(
            self.opt, num_warmup_steps=self.hparams.warmup_steps, num_training_steps=t_total
        )
        return dataloader

    def val_dataloader(self):
        return self.load_dataset("dev", self.hparams.eval_batch_size)

    def test_dataloader(self):
        return self.load_dataset("test", self.hparams.eval_batch_size)

    def _feature_file(self, mode):
        return os.path.join(
            self.hparams.data_dir,
            "cached_{}_{}_{}".format(
                mode,
                list(filter(None, self.hparams.model_name_or_path.split("/"))).pop(),
                str(self.hparams.max_seq_length)))

    def get_str(self) -> str:
        model_str = f'{self.hparams.model_type}'
        if self.hparams.baseline:
            model_str += '_baseline'
        return model_str

class LoggingCallback(pl.Callback):
    """Class for logging callbacks."""

    @rank_zero_only
    def on_validation_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
        print("***** Validation results *****")
        # Log results
        print(absa_utils.tabular(trainer.callback_metrics, 'Metrics'))

    @rank_zero_only
    def on_test_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule):
        print("***** Test results *****")
        # Log results
        print(absa_utils.tabular(trainer.callback_metrics, 'Metrics'))

        log_dir = Path(trainer.logger.experiment.log_dir)
        with open(log_dir / 'sent_f1.txt', 'w', encoding='utf-8') as f1_file:
            f1_file.writelines([f'{v}\n' for v in pl_module.sentence_metrics['f1']])
