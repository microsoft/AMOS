# Copyright (c) Microsoft Corporation. 
# Licensed under the MIT license.
"""
Pretraining Text Encoders with Adversarial Mixture of Training Signal Generators
"""

import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Function

from fairseq import utils
from fairseq.models import (
    FairseqEncoder,
    FairseqEncoderModel,
    register_model,
    register_model_architecture,
)
from fairseq.modules import (
    LayerNorm,
    TransformerSentenceEncoder,
)

from fairseq.models.squad import SQuADHead

from fairseq.modules.transformer_sentence_encoder import init_bert_params
from fairseq.modules.quant_noise import quant_noise as apply_quant_noise_

logger = logging.getLogger(__name__)


@register_model('amos')
class AMOS_Model(FairseqEncoderModel):

    def __init__(self, args, auxiliary, main_encoder):
        super().__init__(main_encoder)
        self.auxiliary = auxiliary
        self.args = args

        # We follow BERT's random weight initialization
        self.apply(init_bert_params)

        self.classification_heads = nn.ModuleDict()

    @staticmethod
    def add_args(parser):
        """Add model-specific arguments to the parser."""
        parser.add_argument('--encoder-layers', type=int, metavar='L',
                            help='num encoder layers')
        parser.add_argument('--encoder-embed-dim', type=int, metavar='H',
                            help='encoder embedding dimension')
        parser.add_argument('--encoder-ffn-embed-dim', type=int, metavar='F',
                            help='encoder embedding dimension for FFN')
        parser.add_argument('--encoder-attention-heads', type=int, metavar='A',
                            help='num encoder attention heads')
        parser.add_argument('--generator-sample-mode', choices=['train', 'eval', 'zero-dropout'],
                            help='which mode the generator is in when sampling from its MLM output')
        parser.add_argument('--generator-mlm-layers', type=str,
                            help='a list containing the MLM layer indices in the generator')
        parser.add_argument('--activation-fn',
                            choices=utils.get_available_activation_fns(),
                            help='activation function to use')
        parser.add_argument('--pooler-activation-fn',
                            choices=utils.get_available_activation_fns(),
                            help='activation function to use for pooler layer')
        parser.add_argument('--encoder-normalize-before', action='store_true',
                            help='apply layernorm before each encoder block')
        parser.add_argument('--dropout', type=float, metavar='D',
                            help='dropout probability')
        parser.add_argument('--attention-dropout', type=float, metavar='D',
                            help='dropout probability for attention weights')
        parser.add_argument('--activation-dropout', type=float, metavar='D',
                            help='dropout probability after activation in FFN')
        parser.add_argument('--pooler-dropout', type=float, metavar='D',
                            help='dropout probability in the masked_lm pooler layers')
        parser.add_argument('--max-positions', type=int,
                            help='number of positional embeddings to learn')
        parser.add_argument('--load-checkpoint-heads', action='store_true',
                            help='(re-)register and load heads when loading checkpoints')
        # args for "Reducing Transformer Depth on Demand with Structured Dropout" (Fan et al., 2019)
        parser.add_argument('--encoder-layerdrop', type=float, metavar='D', default=0,
                            help='LayerDrop probability for encoder')
        parser.add_argument('--encoder-layers-to-keep', default=None,
                            help='which layers to *keep* when pruning as a comma-separated list')
        # args for Training with Quantization Noise for Extreme Model Compression ({Fan*, Stock*} et al., 2020)
        parser.add_argument('--quant-noise-pq', type=float, metavar='D', default=0,
                            help='iterative PQ quantization noise at training time')
        parser.add_argument('--quant-noise-pq-block-size', type=int, metavar='D', default=8,
                            help='block size of quantization noise at training time')
        parser.add_argument('--quant-noise-scalar', type=float, metavar='D', default=0,
                            help='scalar quantization noise and scalar quantization at training time')
        parser.add_argument('--rel-pos', type=int, 
                            help='whether to use relative position or not; 0 = not use; 1 = use')
        parser.add_argument('--checkpoint-activations', action='store_true',
                            help='checkpoint activations at each layer, which saves GPU '
                                 'memory usage at the cost of some additional compute')
        parser.add_argument('--offload-activations', action='store_true',
                            help='checkpoint activations at each layer, then save to gpu. Sets --checkpoint-activations.')
        parser.add_argument('--gumbel-softmax-temperature', type=float,
                            help='temperature for Gumbel-Softmax')
        parser.add_argument('--disc-grad-mul', type=str,
                            help='gradient multiplier backpropagated from the discriminator')
        parser.add_argument('--mask-cls', action='store_true',
                            help='has probability to mask cls')
        parser.add_argument('--binary-loss-weight', type=float,
                            help='loss weight for the binary loss')
        parser.add_argument('--rel-pos-bins', type=int, 
                            help='number of relative position buckets')
        parser.add_argument('--max-rel-pos', type=int, 
                            help='max relative positions')

    @classmethod
    def build_model(cls, args, task):
        """Build a new model instance."""

        # make sure all arguments are present
        base_architecture(args)

        if not hasattr(args, 'max_positions'):
            args.max_positions = args.tokens_per_sample

        main_encoder = DiscEncoder(args, task.source_dictionary)
        if args.task == 'amos':
            auxiliary = Generator(args, task.source_dictionary, main_encoder)
        else:
            auxiliary = None
        return cls(args, auxiliary, main_encoder)


    def forward(self, src_tokens, features_only=False, return_all_hiddens=False, classification_head_name=None,
                masked_tokens=None, targets=None, **kwargs):
        if classification_head_name is not None:
            features_only = True

        def get_padding_mask(tokens):
            padding_mask = tokens.eq(self.encoder.sentence_encoder.padding_idx)
            if not padding_mask.any():
                padding_mask = None
            return padding_mask

        padding_mask = get_padding_mask(src_tokens)
        replace_tokens = None
        # in pretraining
        if not features_only:
            small_gen_x_mask_dict, mixture_logits, _ = self.auxiliary(
                src_tokens,
                features_only=False,
                return_all_hiddens=True,
                padding_mask=padding_mask,
                masked_tokens=masked_tokens,
                **kwargs
            )  # Float[num_masked, vocab]

            small_gen_x_masks = []
            layer_mix_logits = []
            layer_idx = []
            for layer in small_gen_x_mask_dict:
                small_gen_x_masks.append(small_gen_x_mask_dict[layer].unsqueeze(-1))
                layer_mix_logits.append(mixture_logits[layer])
                layer_idx.append(layer)
            small_gen_x_masks = torch.cat(small_gen_x_masks, dim=-1)
            layer_mix_logits = torch.cat(layer_mix_logits, dim=-1)
            layer_mix_probs = F.softmax(layer_mix_logits.float(), dim=-1).to(layer_mix_logits)
            sample_logits = torch.matmul(small_gen_x_masks.detach(), layer_mix_probs.unsqueeze(-1)).squeeze(-1)
            sample_probs = F.gumbel_softmax(sample_logits.float(), tau=self.args.gumbel_softmax_temperature, hard=True, dim=-1).to(sample_logits)
            sampled_input = sample_probs.argmax(dim=-1)
            sample_probs = grad_multiply(sample_probs, lambd=self.args.disc_grad_mul)
            src_tokens = src_tokens.clone()
            src_tokens[masked_tokens] = sampled_input
            replace_tokens = (src_tokens != targets)
            emb = self.encoder.sentence_encoder.embed_tokens.weight
            token_embeddings = self.encoder.sentence_encoder.embed_tokens(src_tokens)
            sampled_embeddings = torch.matmul(sample_probs, emb)
            token_embeddings[masked_tokens] = sampled_embeddings
        else:
            token_embeddings = None

        gen_x, extra = self.encoder(
            src_tokens,
            token_embeddings=token_embeddings,
            features_only=features_only,
            return_all_hiddens=return_all_hiddens,
            padding_mask=padding_mask,
            **kwargs
        )

        if classification_head_name is not None:
            gen_x = self.classification_heads[classification_head_name](gen_x)

        if self.args.task == 'amos':
            binary_target = ~replace_tokens
            if padding_mask is not None:
                binary_target = binary_target[~padding_mask]
            return small_gen_x_mask_dict, gen_x, binary_target, replace_tokens, extra
        else:
            return gen_x, extra

    def get_normalized_probs(self, net_output, log_probs, sample=None):
        """Get normalized probabilities (or log probs) from a net's output."""
        logits = net_output[0].float()
        if log_probs:
            return F.log_softmax(logits, dim=-1)
        else:
            return F.softmax(logits, dim=-1)

    def register_classification_head(self, name, num_classes=None, inner_dim=None, **kwargs):
        """Register a classification head."""
        if name in self.classification_heads:
            prev_num_classes = self.classification_heads[name].out_proj.out_features
            prev_inner_dim = self.classification_heads[name].dense.out_features
            if num_classes != prev_num_classes or inner_dim != prev_inner_dim:
                logger.warning(
                    're-registering head "{}" with num_classes {} (prev: {}) '
                    'and inner_dim {} (prev: {})'.format(
                        name, num_classes, prev_num_classes, inner_dim, prev_inner_dim
                    )
                )
        self.classification_heads[name] = AMOS_ClassificationHead(
            self.args.encoder_embed_dim,
            inner_dim or self.args.encoder_embed_dim,
            num_classes,
            self.args.pooler_activation_fn,
            self.args.pooler_dropout,
            self.args.quant_noise_pq,
            self.args.quant_noise_pq_block_size,
        )

    def register_question_answering_head(self, name, num_classes=None):
        self.classification_heads[name] = SQuADHead(
            self.args.encoder_embed_dim,
        )

    @property
    def supported_targets(self):
        return {'self'}

    def upgrade_state_dict_named(self, state_dict, name):
        prefix = name + '.' if name != '' else ''

        # rename decoder -> encoder before upgrading children modules
        for k in list(state_dict.keys()):
            if k.startswith(prefix + 'decoder'):
                new_k = prefix + 'encoder' + k[len(prefix + 'decoder'):]
                state_dict[new_k] = state_dict[k]
                del state_dict[k]

        # upgrade children modules
        super().upgrade_state_dict_named(state_dict, name)

        # Handle new classification heads present in the state dict.
        current_head_names = (
            [] if not hasattr(self, 'classification_heads')
            else self.classification_heads.keys()
        )
        keys_to_delete = []
        for k in state_dict.keys():
            if not k.startswith(prefix + 'classification_heads.'):
                continue

            head_name = k[len(prefix + 'classification_heads.'):].split('.')[0]
            num_classes = state_dict[prefix + 'classification_heads.' + head_name + '.out_proj.weight'].size(0)
            inner_dim = state_dict[prefix + 'classification_heads.' + head_name + '.dense.weight'].size(0)

            if getattr(self.args, 'load_checkpoint_heads', False):
                if head_name not in current_head_names:
                    self.register_classification_head(head_name, num_classes, inner_dim)
            else:
                if head_name not in current_head_names:
                    logger.warning(
                        'deleting classification head ({}) from checkpoint '
                        'not present in current model: {}'.format(head_name, k)
                    )
                    keys_to_delete.append(k)
                elif (
                    num_classes != self.classification_heads[head_name].out_proj.out_features
                    or inner_dim != self.classification_heads[head_name].dense.out_features
                ):
                    logger.warning(
                        'deleting classification head ({}) from checkpoint '
                        'with different dimensions than current model: {}'.format(head_name, k)
                    )
                    keys_to_delete.append(k)
        for k in keys_to_delete:
            del state_dict[k]

        # Copy any newly-added classification heads into the state dict
        # with their current weights.
        if hasattr(self, 'classification_heads'):
            cur_state = self.classification_heads.state_dict()
            for k, v in cur_state.items():
                if prefix + 'classification_heads.' + k not in state_dict:
                    logger.info('Overwriting ' + prefix + 'classification_heads.' + k)
                    state_dict[prefix + 'classification_heads.' + k] = v


class GradMultiply(Function):
    """Gradient backpropagation multiplication."""

    @staticmethod
    def forward(ctx, x, lambd):
        ctx.lambd = lambd
        return x.view_as(x)
    
    @staticmethod
    def backward(ctx, grad_output):
        return (grad_output * ctx.lambd), None

def grad_multiply(x, lambd=-1):
    return GradMultiply.apply(x, lambd)


class MaskedLMHead(nn.Module):
    """Head for masked language modeling."""

    def __init__(self, hidden_dim, embed_dim, output_dim, activation_fn, weight, bias=None):
        super().__init__()
        self.dense = nn.Linear(hidden_dim, embed_dim)
        self.activation_fn = utils.get_activation_fn(activation_fn)
        self.layer_norm = LayerNorm(embed_dim)
        self.weight = weight
        self.bias = nn.Parameter(torch.zeros(output_dim)) if bias is None else bias
        self.layer_mixture = nn.Linear(hidden_dim, 1, bias=False)

    def forward(self, features, masked_tokens=None, **kwargs):
        # Only project the masked tokens while training,
        # saves both memory and computation
        if masked_tokens is not None:
            features = features[masked_tokens, :]

        mixture_logit = self.layer_mixture(features)
        x = self.dense(features)
        x = self.activation_fn(x)
        x = self.layer_norm(x)
        # project back to size of vocabulary with bias
        x = F.linear(x, self.weight, bias=self.bias)
        return x, mixture_logit


class BinaryHead(nn.Module):
    """Head for masked language modeling."""

    def __init__(self, embed_dim, activation_fn):
        super().__init__()
        self.embed_dim = embed_dim
        # Todo: check projection is needed or not
        # self.dense = nn.Linear(embed_dim, embed_dim)
        # self.activation_fn = utils.get_activation_fn(activation_fn)
        # self.layer_norm = LayerNorm(embed_dim)

        self.out_proj = nn.Linear(embed_dim, 1, bias=True)
        # self.out_proj.bias.data.zero_()

    def forward(self, x, padding_mask=None, **kwargs):
        # Only project the unmasked tokens while training,
        # saves both memory and computation
        if padding_mask is not None:
            x = x[~padding_mask, :]

        # x = self.dense(x)
        # x = self.activation_fn(x)
        # x = self.layer_norm(x)
        return self.out_proj(x)


class AMOS_ClassificationHead(nn.Module):
    """Head for sentence-level classification tasks."""

    def __init__(self, input_dim, inner_dim, num_classes, activation_fn, pooler_dropout, q_noise=0, qn_block_size=8):
        super().__init__()
        self.dense = nn.Linear(input_dim, inner_dim)
        self.activation_fn = utils.get_activation_fn(activation_fn)
        self.dropout = nn.Dropout(p=pooler_dropout)
        self.out_proj = apply_quant_noise_(
            nn.Linear(inner_dim, num_classes), q_noise, qn_block_size
        )

    def forward(self, features, **kwargs):
        x = features[:, 0, :]  # take <s> token (equiv. to [CLS])
        x = self.dropout(x)
        x = self.dense(x)
        x = self.activation_fn(x)
        x = self.dropout(x)
        x = self.out_proj(x)
        return x


class Generator(FairseqEncoder):
    """AMOS auxiliary encoder.

    Implements the :class:`~fairseq.models.FairseqEncoder` interface required
    by :class:`~fairseq.models.FairseqLanguageModel`.
    """

    def __init__(self, args, dictionary, main_encoder):
        super().__init__(dictionary)
        self.args = args
        self.generator_mlm_layers = eval(args.generator_mlm_layers)
        self.sentence_encoder = TransformerSentenceEncoder(
            padding_idx=dictionary.pad(),
            vocab_size=len(dictionary),
            num_encoder_layers=max(self.generator_mlm_layers),
            embedding_dim=int(args.encoder_embed_dim),
            ffn_embedding_dim=int(args.encoder_ffn_embed_dim),
            num_attention_heads=int(args.encoder_attention_heads),
            dropout=args.dropout if args.generator_sample_mode != "zero-dropout" else 0,
            attention_dropout=args.attention_dropout if args.generator_sample_mode != "zero-dropout" else 0,
            activation_dropout=args.activation_dropout if args.generator_sample_mode != "zero-dropout" else 0,
            layerdrop=args.encoder_layerdrop,
            max_seq_len=args.max_positions,
            num_segments=0,
            encoder_normalize_before=False,
            apply_bert_init=True,
            activation_fn=args.activation_fn,
            q_noise=args.quant_noise_pq,
            qn_block_size=args.quant_noise_pq_block_size,
            rel_pos=args.rel_pos,
            checkpoint_activations=args.checkpoint_activations,
            offload_activations=args.offload_activations,
            share_embed_tokens=main_encoder.sentence_encoder.embed_tokens,
            share_embed_positions=main_encoder.sentence_encoder.embed_positions if args.generator_sample_mode != "zero-dropout" else None,
            share_emb_layer_norm=main_encoder.sentence_encoder.emb_layer_norm if args.generator_sample_mode != "zero-dropout" else None, 
            shared_embedding_dim=args.encoder_embed_dim,
            rel_pos_bins=args.rel_pos_bins,
            max_rel_pos=args.max_rel_pos,
            grad_detach_layer=self.generator_mlm_layers,
        )
        self.lm_head = MaskedLMHead(
            hidden_dim=int(args.encoder_embed_dim),
            embed_dim=int(args.encoder_embed_dim),
            output_dim=len(dictionary),
            activation_fn=args.activation_fn,
            weight=main_encoder.sentence_encoder.embed_tokens.weight,
        )

    def forward(self, src_tokens, features_only=False, return_all_hiddens=False, padding_mask=None, masked_tokens=None, **unused):
        """
        Args:
            src_tokens (LongTensor): input tokens of shape `(batch, src_len)`
            features_only (bool, optional): skip LM head and just return
                features. If True, the output will be of shape
                `(batch, src_len, embed_dim)`.
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).

        Returns:
            tuple:
                - the LM output of shape `(batch, src_len, vocab)`
                - a dictionary of additional data, where 'inner_states'
                  is a list of hidden states.
        """
        all_features, extra = self.extract_features(src_tokens, return_all_hiddens, padding_mask)
        if not features_only:
            all_x = {}
            all_mixture_logits = {}
            for layer in all_features:
                x, sel_logit = self.output_layer(all_features[layer], masked_tokens=masked_tokens)
                all_x[layer] = x
                all_mixture_logits[layer] = sel_logit
        return all_x, all_mixture_logits, extra

    def extract_features(self, src_tokens, return_all_hiddens=False, padding_mask=None, **unused):
        inner_states, _ = self.sentence_encoder(
            src_tokens,
            last_state_only=not return_all_hiddens,
            use_ext_padding_mask=True,
            padding_mask=padding_mask
        )
        all_features = {}
        for layer in self.generator_mlm_layers:
            all_features[layer] = inner_states[layer].transpose(0, 1)
        return all_features, {'inner_states': inner_states if return_all_hiddens else None}

    def output_layer(self, features, masked_tokens=None, **unused):
        return self.lm_head(features, masked_tokens)

    def max_positions(self):
        """Maximum output length supported by the encoder."""
        return self.args.max_positions


class DiscEncoder(FairseqEncoder):
    """AMOS main encoder (discriminator).

    Implements the :class:`~fairseq.models.FairseqEncoder` interface required
    by :class:`~fairseq.models.FairseqLanguageModel`.
    """

    def __init__(self, args, dictionary):
        super().__init__(dictionary)
        self.args = args
        if args.encoder_layers_to_keep:
            args.encoder_layers = len(args.encoder_layers_to_keep.split(","))

        self.sentence_encoder = TransformerSentenceEncoder(
            padding_idx=dictionary.pad(),
            vocab_size=len(dictionary),
            num_encoder_layers=args.encoder_layers,
            embedding_dim=args.encoder_embed_dim,
            ffn_embedding_dim=args.encoder_ffn_embed_dim,
            num_attention_heads=args.encoder_attention_heads,
            dropout=args.dropout,
            attention_dropout=args.attention_dropout,
            activation_dropout=args.activation_dropout,
            layerdrop=args.encoder_layerdrop,
            max_seq_len=args.max_positions,
            num_segments=0,
            encoder_normalize_before=False,
            apply_bert_init=True,
            activation_fn=args.activation_fn,
            q_noise=args.quant_noise_pq,
            qn_block_size=args.quant_noise_pq_block_size,
            rel_pos=args.rel_pos,
            checkpoint_activations=args.checkpoint_activations,
            offload_activations=args.offload_activations,
            rel_pos_bins=args.rel_pos_bins,
            max_rel_pos=args.max_rel_pos
        )
        self.binary_head = BinaryHead(
            embed_dim=int(args.encoder_embed_dim),
            activation_fn=args.activation_fn,
        )

    def forward(self, src_tokens, features_only=False, return_all_hiddens=False, padding_mask=None, token_embeddings=None, **unused):
        """
        Args:
            src_tokens (LongTensor): input tokens of shape `(batch, src_len)`
            features_only (bool, optional): skip LM head and just return
                features. If True, the output will be of shape
                `(batch, src_len, embed_dim)`.
            return_all_hiddens (bool, optional): also return all of the
                intermediate hidden states (default: False).

        Returns:
            tuple:
                - the LM output of shape `(batch, src_len, vocab)`
                - a dictionary of additional data, where 'inner_states'
                  is a list of hidden states.
        """
        x, extra = self.extract_features(src_tokens, return_all_hiddens, padding_mask, token_embeddings)
        if not features_only:
            x = self.output_layer(x, padding_mask=padding_mask)
        return x, extra

    def extract_features(self, src_tokens, return_all_hiddens=False, padding_mask=None, token_embeddings=None, **unused):
        inner_states, _ = self.sentence_encoder(
            src_tokens,
            last_state_only=not return_all_hiddens,
            use_ext_padding_mask=True,
            padding_mask=padding_mask,
            token_embeddings=token_embeddings,
        )
        features = inner_states[-1]
        return features, {'inner_states': inner_states if return_all_hiddens else None}

    def output_layer(self, features, padding_mask=None, **unused):
        return self.binary_head(features, padding_mask=padding_mask)

    def max_positions(self):
        """Maximum output length supported by the encoder."""
        return self.args.max_positions


@register_model_architecture('amos', 'amos')
def base_architecture(args):
    args.encoder_layers = getattr(args, 'encoder_layers', 12)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 768)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 3072)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 12)
    args.generator_mlm_layers = getattr(args, 'generator_mlm_layers', '[4,6,8]')
    args.generator_sample_mode = getattr(args, 'generator_sample_mode', 'zero-dropout')

    args.activation_fn = getattr(args, 'activation_fn', 'gelu')
    args.pooler_activation_fn = getattr(args, 'pooler_activation_fn', 'tanh')

    args.dropout = getattr(args, 'dropout', 0.1)
    args.attention_dropout = getattr(args, 'attention_dropout', 0.1)
    args.activation_dropout = getattr(args, 'activation_dropout', 0.0)
    args.pooler_dropout = getattr(args, 'pooler_dropout', 0.0)
    args.encoder_layers_to_keep = getattr(args, 'encoder_layers_to_keep', None)
    args.encoder_layerdrop = getattr(args, 'encoder_layerdrop', 0.0)
    args.rel_pos = getattr(args, 'rel_pos', 1)
    args.binary_loss_weight = getattr(args, 'binary_loss_weight', 50)
    args.mask_cls = getattr(args, 'mask_cls', False)
    args.rel_pos_bins = getattr(args, 'rel_pos_bins', 32)
    args.max_rel_pos = getattr(args, 'max_rel_pos', 128)

    args.checkpoint_activations = getattr(args, "checkpoint_activations", False)
    args.offload_activations = getattr(args, "offload_activations", False)
    if args.offload_activations:
        args.checkpoint_activations = True

    # Adversarial Training
    args.gumbel_softmax_temperature = getattr(args, 'gumbel_softmax_temperature', 0.3)
    args.disc_grad_mul = getattr(args, 'disc_grad_mul', -1)

@register_model_architecture('amos', 'amos_base')
def amos_base_architecture(args):
    base_architecture(args)


@register_model_architecture('amos', 'amos_small')
def amos_small_architecture(args):
    args.encoder_layers = getattr(args, 'encoder_layers', 12)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 256)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 1024)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 4)

    base_architecture(args)


@register_model_architecture('amos', 'amos_large')
def amos_large_architecture(args):
    args.encoder_layers = getattr(args, 'encoder_layers', 24)
    args.encoder_embed_dim = getattr(args, 'encoder_embed_dim', 1024)
    args.encoder_ffn_embed_dim = getattr(args, 'encoder_ffn_embed_dim', 4096)
    args.encoder_attention_heads = getattr(args, 'encoder_attention_heads', 16)
    base_architecture(args)
