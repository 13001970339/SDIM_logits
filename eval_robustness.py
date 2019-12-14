from __future__ import print_function
import argparse
import os
import sys
import time

import numpy as np

import torch
from torch.optim import Adam
from torch.utils.data import DataLoader, Dataset
import torchvision.transforms as transforms

import models
from sdim_ce import SDIM
from utils import cal_parameters, get_dataset, AverageMeter


def load_pretrained_model(hps):
    checkpoint_path = '{}_{}.pth'.format(hps.classifier_name, hps.problem)
    print('Load pre-trained checkpoint: {}'.format(checkpoint_path))
    pre_trained_dir = os.path.join(hps.log_dir, checkpoint_path)

    model = models.ResNet34(num_c=hps.n_classes)
    model.load_state_dict(torch.load(pre_trained_dir, map_location=lambda storage, loc: storage))
    return model


def get_c_dataset(dir='data/CIFAR-10-C'):
    from os import listdir
    files = [file for file in listdir(dir) if file != 'labels.npy']

    y = np.load(os.path.join(dir, 'labels.npy'))
    for file in files:
        yield file.split('.')[0], np.load(file), y


class CorruptionDataset(Dataset):
    def __init__(self, x, y, transform=None):
        """

        :param x: numpy array
        :param y: numpy array
        """
        assert x.shape[0] == y.shape[0]
        self.x = x
        self.y = y
        self.transform = transform

    def __getitem__(self, item):
        sample = self.x[item]
        if self.transform:
            sample = self.transform(sample)
        return sample, self.y[item]

    def __len__(self):
        return self.x.shape[0]


def inference_rejection(sdim, hps):
    torch.manual_seed(hps.seed)
    np.random.seed(hps.seed)

    name = 'SDIM_{}_{}.pth'.format(hps.classifier_name, hps.problem)
    checkpoint_path = os.path.join(hps.log_dir, name)

    sdim.load_state_dict(torch.load(checkpoint_path, map_location=lambda storage, loc: storage)['state'])
    sdim.eval()

    # Get thresholds
    threshold_list = []
    for label_id in range(hps.n_classes):
        # No data augmentation(crop_flip=False) when getting in-distribution thresholds
        dataset = get_dataset(data_name=hps.problem, train=True, label_id=label_id, crop_flip=False)
        in_test_loader = DataLoader(dataset=dataset, batch_size=hps.n_batch_test, shuffle=False)

        print('Inference on {}, label_id {}'.format(hps.problem, label_id))
        in_ll_list = []
        for batch_id, (x, y) in enumerate(in_test_loader):
            x = x.to(hps.device)
            y = y.to(hps.device)
            ll = sdim(x)

            correct_idx = ll.argmax(dim=1) == y

            ll_, y_ = ll[correct_idx], y[correct_idx]  # choose samples are classified correctly
            in_ll_list += list(ll_[:, label_id].detach().cpu().numpy())

        thresh_idx = int(hps.percentile * len(in_ll_list))
        thresh = sorted(in_ll_list)[thresh_idx]
        print('threshold_idx/total_size: {}/{}, threshold: {:.3f}'.format(thresh_idx, len(in_ll_list), thresh))
        threshold_list.append(thresh)  # class mean as threshold

    # Evaluation
    thresholds = torch.tensor(threshold_list).to(hps.device)

    transform = transforms.Compose([transforms.ToTensor(),
                                    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))])
    interval = 10000
    for corruption_id, (corruption_type, data, labels) in get_c_dataset():
        print('Corruption type: {}'.format(corruption_type))

        for severity in range(5):
            x_severity = data[severity * interval: (severity + 1) * interval]
            y_severity = labels[severity * interval: (severity + 1) * interval]

            dataset = CorruptionDataset(x_severity, y_severity, transform=transform)
            test_loader = DataLoader(dataset=dataset, batch_size=hps.n_batch_test, shuffle=False, num_workers=4)

            n_correct = 0
            n_false = 0
            n_reject = 0

            for batch_id, (x, target) in enumerate(test_loader):
                # Note that images are scaled to [-1.0, 1.0]
                x, target = x.to(hps.device), target.to(hps.device)

                with torch.no_grad():
                    log_lik = sdim(x)

                values, pred = log_lik.max(dim=1)
                confidence_idx = values >= thresholds[pred]  # the predictions you have confidence in.
                reject_idx = values < thresholds[pred]       # the ones rejected.

                n_correct += pred[confidence_idx].eq(target[confidence_idx]).sum().item()
                n_false += (pred[confidence_idx] != target[confidence_idx]).sum().item()
                n_reject += reject_idx.float().sum().item()

            n = len(test_loader.dataset)
            acc = n_correct / n
            false_rate = n_false / n
            reject_rate = n_reject / n

            acc_remain = acc / (acc + false_rate)

            print('Test set:\nacc: {:.4f}, false rate: {:.4f}, reject rate: {:.4f}'.format(acc, false_rate, reject_rate))
            print('acc on remain set: {:.4f}'.format(acc_remain))

        exit(0)
    return acc, reject_rate, acc_remain



if __name__ == '__main__':
    # This enables a ctr-C without triggering errors
    import signal

    signal.signal(signal.SIGINT, lambda x, y: sys.exit(0))

    parser = argparse.ArgumentParser(description='PyTorch Implementation of SDIM_logits.')
    parser.add_argument("--verbose", action='store_true', help="Verbose mode")
    parser.add_argument("--inference", action="store_true",
                        help="Used in inference mode")
    parser.add_argument("--rejection_inference", action="store_true",
                        help="Used in inference mode with rejection")
    parser.add_argument("--ood_inference", action="store_true",
                        help="Used in ood inference mode")
    parser.add_argument("--draw", action="store_true",
                        help="Used in draw")
    parser.add_argument("--log_dir", type=str,
                        default='./logs', help="Location to save logs")

    # Dataset hyperparams:
    parser.add_argument("--problem", type=str, default='cifar10',
                        help="Problem cifar10|svhn")
    parser.add_argument("--n_classes", type=int,
                        default=10, help="number of classes of dataset.")
    parser.add_argument("--data_dir", type=str, default='data',
                        help="Location of data")

    # Optimization hyperparams:
    parser.add_argument("--n_batch_train", type=int,
                        default=128, help="Minibatch size")
    parser.add_argument("--n_batch_test", type=int,
                        default=200, help="Minibatch size")
    parser.add_argument("--optimizer", type=str,
                        default="adam", help="adam or adamax")
    parser.add_argument("--lr", type=float, default=0.001,
                        help="Base learning rate")
    parser.add_argument("--epochs", type=int, default=20,
                        help="Total number of training epochs")

    # Inference hyperparams:
    parser.add_argument("--percentile", type=float, default=0.01,
                        help="percentile value for inference with rejection.")

    # sdim hyperparams:
    parser.add_argument("--image_size", type=int,
                        default=32, help="Image size")
    parser.add_argument("--mi_units", type=int,
                        default=64, help="output size of 1x1 conv network for mutual information estimation")
    parser.add_argument("--rep_size", type=int,
                        default=10, help="size of the global representation from encoder")
    parser.add_argument("--classifier_name", type=str, default='resnet',
                        help="classifier name: resnet|densenet")
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')

    # Ablation
    parser.add_argument("--seed", type=int, default=1234, help="Random seed")
    hps = parser.parse_args()  # So error if typo

    use_cuda = not hps.no_cuda and torch.cuda.is_available()

    torch.manual_seed(hps.seed)

    hps.device = torch.device("cuda" if use_cuda else "cpu")

    # Create log dir
    logdir = os.path.abspath(hps.log_dir) + "/"
    if not os.path.exists(logdir):
        os.mkdir(logdir)

    classifier = load_pretrained_model(hps).to(hps.device)
    sdim = SDIM(disc_classifier=classifier,
                n_classes=hps.n_classes,
                rep_size=hps.rep_size,
                mi_units=hps.mi_units,
                ).to(hps.device)
    optimizer = Adam(sdim.parameters(), lr=hps.lr)

    print('==>  # SDIM parameters: {}.'.format(cal_parameters(sdim)))

    inference_rejection(sdim, hps)

