#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import division

import argparse
import logging
import os
import sys

import numpy as np
import soundfile as sf
import torch
from sklearn.preprocessing import StandardScaler
from torch.autograd import Variable
from torchvision import transforms

from utils import background, find_files, read_hdf5, read_txt
from wavenet import WaveNet, decode_mu_law, encode_mu_law


@background(max_prefetch=1)
def decode_generator(feat_list, wav_transform=None, feat_transform=None, use_speaker_code=False):
    """DECODE BATCH GENERATOR

    Args:
        featdir (str): directory including feat files
        wav_transform (func): preprocessing function for waveform
        feat_transform (func): preprocessing function for aux feats
        use_speaker_code (bool): whether to use speaker code

    Return: generator instance

    """
    # process over all of files
    for featfile in feat_list:
        x = np.zeros((1))
        h = read_hdf5(featfile, "/feat")
        if use_speaker_code:
            sc = read_hdf5(featfile, "/speaker_code")
            sc = np.tile(sc, [h.shape[0], 1])
            h = np.concatenate([h, sc], axis=1)

        # perform pre-processing
        if wav_transform is not None:
            x = wav_transform(x)
        if feat_transform is not None:
            h = feat_transform(h)

        x = x.unsqueeze(0)
        h = h.transpose(0, 1).unsqueeze(0)
        n_samples = h.size(2) - 1
        feat_id = os.path.basename(featfile).replace(".h5", "")

        yield feat_id, (x, h, n_samples)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # path setting
    parser.add_argument("--feats", required=True,
                        type=str, help="list or directory of aux feat files")
    parser.add_argument("--checkpoint", required=True,
                        type=str, help="model file")
    parser.add_argument("--config", required=True,
                        type=str, help="configure file")
    parser.add_argument("--stats", required=True,
                        type=str, help="hdf5 file including statistics")
    parser.add_argument("--outdir", required=True,
                        type=str, help="directory to save generated samples")
    parser.add_argument("--fs", default=16000,
                        type=int, help="sampling rate")
    # other setting
    parser.add_argument("--seed", default=1,
                        type=int, help="seed number")
    parser.add_argument("--verbose", default=1,
                        type=int, help="log level")
    args = parser.parse_args()

    # set log level
    if args.verbose > 0:
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S')
    elif args.verbose > 1:
        logging.basicConfig(level=logging.DEBUG,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S')
    else:
        logging.basicConfig(level=logging.WARN,
                            format='%(asctime)s (%(module)s:%(lineno)d) %(levelname)s: %(message)s',
                            datefmt='%m/%d/%Y %I:%M:%S')
        logging.warn("logging is disabled.")

    # fix seed
    os.environ['PYTHONHASHSEED'] = str(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    # load config
    config = torch.load(args.config)

    # define network
    model = WaveNet(n_quantize=config.n_quantize,
                    n_aux=config.n_aux,
                    n_resch=config.n_resch,
                    n_skipch=config.n_skipch,
                    dilation_depth=config.dilation_depth,
                    dilation_repeat=config.dilation_repeat,
                    kernel_size=config.kernel_size)
    logging.info(model)

    # send to gpu
    if torch.cuda.is_available():
        model.cuda()
    else:
        logging.error("gpu is not available. please check the setting.")
        sys.exit(1)

    # define transforms
    scaler = StandardScaler()
    scaler.mean_ = read_hdf5(args.stats, "/mean")
    scaler.scale_ = read_hdf5(args.stats, "/scale")
    wav_transform = transforms.Compose([
        lambda x: encode_mu_law(x, config.n_quantize),
        lambda x: torch.from_numpy(x).long().cuda(),
        lambda x: Variable(x, volatile=True)])
    feat_transform = transforms.Compose([
        lambda x: scaler.transform(x),
        lambda x: torch.from_numpy(x).float().cuda(),
        lambda x: Variable(x, volatile=True)])

    # define generator
    if os.path.isdir(args.feats):
        feat_list = sorted(find_files(args.feats, "*.h5"))
    else:
        feat_list = read_txt(args.feats)
    generator = decode_generator(feat_list, wav_transform, feat_transform, False)

    # check directory existence
    if os.path.exists(args.outdir):
        os.makedirs(args.outdir)

    # decode
    for feat_id, (x, h, n_samples) in generator:
        logging.info("decoding %s (length = %d)" % (feat_id, n_samples))
        samples = model.fast_generate(x, h, n_samples)
        wav = decode_mu_law(samples, config.n_quantize)
        sf.write(args.outdir + "/" + feat_id + ".wav", wav, args.fs, "PCM_16")
