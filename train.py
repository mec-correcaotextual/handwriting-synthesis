import sys
import os
import argparse
import math
import pickle
import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, SubsetRandomSampler
from torch.optim.lr_scheduler import ReduceLROnPlateau
from tensorboardX import SummaryWriter

from utils import plot_stroke, normalize_data, filter_long_strokes, OneHotEncoder
from utils import plot_phi, plot_attn_scalar
from model import HandWritingRNN, HandWritingSynthRNN
from tqdm.notebook import trange
# ------------------------------------------------------------------------------
import logging

logging.basicConfig(filename='loss.txt', filemode='w', format='%(asctime)s - %(message)s', level=logging.INFO)


class HandWritingData(Dataset):
    """ Takes care of padding; So input is a list of tensors of different length
    """

    def __init__(self, sentences, strokes):
        assert len(sentences) == len(strokes)
        self.len = len(strokes)
        self.sentences = sentences
        self.pad_data(sentences, strokes)

    def pad_data(self, sentences, strokes):
        """
        input:
            strokes: list having N tensors of dimensions (*, d)
        output:
            padded_strokes: tensor of padded sequences of dimension (T, N, d) where
                T is the length of the longest tensor.
            masks: tensor of same dimension as strokes but having value 0 at
                positions where padding was done and value 1 at all other places
        """
        # first pad strokes and create masks corresponding to it
        self.padded_strokes = torch.nn.utils.rnn.pad_sequence(
            strokes, batch_first=True, padding_value=0.0
        )
        self.masks = self.padded_strokes.new_zeros(
            self.len, self.padded_strokes.shape[1]
        )
        for i, s in enumerate(strokes):
            self.masks[i, : s.shape[0]] = 1

        # now pad sentences
        self.padded_sentences = torch.nn.utils.rnn.pad_sequence(
            sentences, batch_first=True, padding_value=0.0
        )
        self.sentence_masks = self.padded_sentences.new_zeros(
            self.len, self.padded_sentences.shape[1]
        )
        for i, s in enumerate(sentences):
            self.sentence_masks[i, : s.shape[0]] = 1

        self.strokes_padded_len = self.padded_strokes.shape[1]
        self.sentences_padded_len = self.padded_sentences.shape[1]

        print(
            "Strokes are padded to length {}, and Sentences are padded to length {}".format(
                self.strokes_padded_len, self.sentences_padded_len
            )
        )

    def __getitem__(self, idx):
        return (
            self.padded_sentences[idx],
            self.padded_strokes[idx],
            self.masks[idx],
            self.sentence_masks[idx],
        )

    def __len__(self):
        return self.len


# ------------------------------------------------------------------------------


def mog_density_2d(x, log_pi, mu, sigma, rho):
    """
    Calculates The probability density of the next input x given the output vector
    as given in Eq. 23, 24 & 25 of the paper
    Expected dimensions of input:
        x : (n, 2)
        log_pi : (n , m)
        mu : (n , m, 2)
        sigma : (n , m, 2)
        rho : (n, m)
    Returns:
        log_densities : (n,)
    """
    x_c = (x.unsqueeze(1) - mu) / sigma

    z = (x_c ** 2).sum(dim=2) - 2 * rho * x_c[:, :, 0] * x_c[:, :, 1]

    log_densities = (
            (-z / (2 * (1 - rho ** 2)))
            - (
                    np.log(2 * math.pi)
                    + sigma[:, :, 0].log()
                    + sigma[:, :, 1].log()
                    + 0.5 * (1 - rho ** 2).log()
            )
            + log_pi
    )
    # dimensions - log_densities : (n, m)

    # using torch log_sum_exp; return tensor of shape (n,)
    log_densities = torch.logsumexp(log_densities, dim=1)

    return log_densities


def criterion(x, e, log_pi, mu, sigma, rho, masks):
    """
    Calculates the sequence loss as given in Eq. 26 of the paper
    Expected dimensions of input:
        x: (n, b, 3)
        e: (n, b)
        log_pi: (n, b, m)
        mu: (n, b, 2*m)
        sigma: (n, b, 2*m)
        rho: (n, b, m),
        masks: (n, b)
    Here n is the sequence length and m in number of components assumed for MoG
    """
    epsillon = 1e-4
    n, b, m = log_pi.shape
    # n = sequence_length, b = batch_size, m = number_of_component_in_MoG

    # change dimensions to (n*b, *) from (n, b, *)
    x = x.contiguous().view(n * b, 3)
    e = e.view(n * b)
    e = e * x[:, 0] + (1 - e) * (1 - x[:, 0])  # e = (x0 == 1) ? e : (1 - e)
    e = (e + epsillon) / (1 + 2 * epsillon)

    x = x[:, 1:3]  # 2-dimensional offset values which is needed for MoG density

    log_pi = log_pi.view(n * b, m)
    mu = mu.view(n * b, m, 2)
    sigma = sigma.view(n * b, m, 2) + epsillon
    rho = rho.view(n * b, m) / (1 + epsillon)

    # add small constant for numerical stability
    log_density = mog_density_2d(x, log_pi, mu, sigma, rho)

    masks = masks.contiguous().view(n * b)
    ll = ((log_density + e.log()) * masks).sum() / masks.sum()
    # ll = ((log_density + e.log()) * masks).sum()
    return -ll


# ------------------------------------------------------------------------------


def train(device, args, data_path="data/"):
    """
    """
    random_seed = 42

    writer = SummaryWriter(log_dir=args.logdir, comment="")

    model_path = args.logdir + (
        "/unconditional_models/" if args.uncond else "/conditional_models/"
    )
    os.makedirs(model_path, exist_ok=True)

    strokes = np.load(data_path + "strokes.npy", encoding="latin1", allow_pickle=True)
    sentences = ""
    with open(data_path + "sentences.txt") as f:
        sentences = f.readlines()
    sentences = [snt.replace("\n", "") for snt in sentences]
    # Instead of removing the newline symbols, should it be used instead?

    MAX_STROKE_LEN = 800
    strokes, sentences, MAX_SENTENCE_LEN = filter_long_strokes(
        strokes, sentences, MAX_STROKE_LEN, max_index=args.n_data
    )
    # print("Max sentence len after filter is: {}".format(MAX_SENTENCE_LEN))

    # dimension of one-hot representation
    N_CHAR = 57
    oh_encoder = OneHotEncoder(sentences, n_char=N_CHAR)
    pickle.dump(oh_encoder, open("data/one_hot_encoder.pkl", "wb"))
    sentences_oh = [s.to(device) for s in oh_encoder.one_hot(sentences)]

    # normalize strokes data and convert to pytorch tensors
    strokes = normalize_data(strokes)
    # plot_stroke(strokes[1])
    tstrokes = [torch.from_numpy(stroke).to(device) for stroke in strokes]

    # pytorch dataset
    dataset = HandWritingData(sentences_oh, tstrokes)

    # validating the padding lengths
    assert dataset.strokes_padded_len <= MAX_STROKE_LEN
    assert dataset.sentences_padded_len == MAX_SENTENCE_LEN

    # train - validation split
    train_split = 0.95
    train_size = int(train_split * len(dataset))
    validn_size = len(dataset) - train_size
    dataset_train, dataset_validn = torch.utils.data.random_split(
        dataset, [train_size, validn_size]
    )

    dataloader_train = DataLoader(
        dataset_train, batch_size=args.batch_size, shuffle=True, drop_last=False
    )  # last batch may be smaller than batch_size
    dataloader_validn = DataLoader(
        dataset_validn, batch_size=args.batch_size, shuffle=True, drop_last=False
    )

    common_model_structure = {"memory_cells": 400, "n_gaussians": 20, "num_layers": 2}
    model = (
        HandWritingRNN(**common_model_structure).to(device)
        if args.uncond
        else HandWritingSynthRNN(
            n_char=N_CHAR,
            n_gaussians_window=10,
            kappa_factor=0.05,
            **common_model_structure,
        ).to(device)
    )
    print(model)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3, weight_decay=0)
    # optimizer = torch.optim.RMSprop(model.parameters(), lr=1e-2,
    #                                   weight_decay=0, momentum=0)

    if args.resume is None:
        model.init_params()
    else:
        model.load_state_dict(torch.load(args.resume, map_location=device))
        print("Resuming trainig on {}".format(args.resume))
        # resume_optim_file = args.resume.split(".pt")[0] + "_optim.pt"
        # if os.path.exists(resume_optim_file):
        #     optimizer = torch.load(resume_optim_file, map_location=device)

    scheduler = ReduceLROnPlateau(
        optimizer, mode="min", factor=0.1 ** 0.5, patience=10, verbose=True
    )

    best_batch_loss = 1e7
    for epoch in trange(10):

        train_losses = []
        validation_iters = []
        validation_losses = []

        for i, (c_seq, x, masks, c_masks) in enumerate(dataloader_train):

            # make batch_first = false
            x = x.permute(1, 0, 2)
            masks = masks.permute(1, 0)

            # remove last point (prepending a dummy point (zeros) already done in data)
            inp_x = x[:-1]  # shape : (T, B, 3)
            masks = masks[:-1]  # shape: (B, T)
            # c_seq.shape: (B, MAX_SENTENCE_LEN, n_char), c_masks.shape: (B, MAX_SENTENCE_LEN)
            inputs = (inp_x, c_seq, c_masks)
            if args.uncond:
                inputs = (inp_x,)

            e, log_pi, mu, sigma, rho, *_ = model(*inputs)

            # remove first point from x to make it y
            loss = criterion(x[1:], e, log_pi, mu, sigma, rho, masks)
            train_losses.append(loss.detach().cpu().numpy())

            optimizer.zero_grad()

            loss.backward()

            # --- this may not be needed
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5)

            optimizer.step()

            # do logging

            logging.info("{},\t".format(loss))
            if i % 10 == 0:
                writer.add_scalar(
                    "Every_10th_batch_loss", loss, epoch * len(dataloader_train) + i
                )

            # save as best model if loss is better than previous best
            if loss < best_batch_loss:
                best_batch_loss = loss
                model_file = (
                        model_path
                        + f"handwriting_{('un' if args.uncond else '')}cond_best.pt"
                )
                torch.save(model.state_dict(), model_file)
                optim_file = model_file.split(".pt")[0] + "_optim.pt"
                torch.save(optimizer, optim_file)

        epoch_avg_loss = np.array(train_losses).mean()
        scheduler.step(epoch_avg_loss)

        # ======================== do the per-epoch logging ========================
        writer.add_scalar("Avg_loss_for_epoch", epoch_avg_loss, epoch)
        print(f"Average training-loss for epoch {epoch} is: {epoch_avg_loss}")

        model_file = (
                model_path + f"handwriting_{('un' if args.uncond else '')}cond_ep{epoch}.pt"
        )
        torch.save(model.state_dict(), model_file)
        optim_file = model_file.split(".pt")[0] + "_optim.pt"
        torch.save(optimizer, optim_file)

        # generate samples from model
        sample_count = 3
        sentences = ["welcome to lyrebird"] + ["abcd efgh vicki"] * (sample_count - 1)
        sentences = [s.to(device) for s in oh_encoder.one_hot(sentences)]

        if args.uncond:
            generated_samples = model.generate(600, batch=sample_count, device=device)
        else:
            generated_samples, attn_vars = model.generate(sentences, device=device)

        figs = []
        # save png files of the generated models
        for i in trange(sample_count):
            f = plot_stroke(
                generated_samples[:, i, :].cpu().numpy(),
                save_name=args.logdir
                          + "/training_imgs/{}cond_ep{}_{}.png".format(
                    ("un" if args.uncond else ""), epoch, i
                ),
            )
            figs.append(f)

        for i, f in enumerate(figs):
            writer.add_figure(f"samples/image_{i}", f, epoch)

        if not args.uncond:
            figs_phi = plot_phi(attn_vars["phi_list"])
            figs_kappa = plot_attn_scalar(attn_vars["kappa_list"])
            for i, (f_phi, f_kappa) in enumerate(zip(figs_phi, figs_kappa)):
                writer.add_figure(f"attention/phi_{i}", f_phi, epoch)
                writer.add_figure(f"attention/kappa_{i}", f_kappa, epoch)


def main():
    parser = argparse.ArgumentParser(description="Train a handwriting generation model")
    parser.add_argument(
        "--uncond",
        help="If want to train the unconditional model",
        action="store_const",
        const=True,
        default=False,
    )
    parser.add_argument(
        "--batch_size", help="Batch size for training", type=int, default=16
    )
    parser.add_argument(
        "--resume",
        help="model path from which to resume training",
        type=str,
        default=None,
    )
    parser.add_argument(
        "--logdir",
        help="Directory to be used for logging",
        type=str,
        default="runs/test/",
    )
    parser.add_argument("--n_data", help="count of strokes to take from data", type=int)

    args = parser.parse_args()

    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if use_cuda else "cpu")

    if use_cuda:
        torch.cuda.empty_cache()

    np.random.seed(101)
    torch.random.manual_seed(101)

    # training
    train(device=device, args=args)


if __name__ == "__main__":
    main()
