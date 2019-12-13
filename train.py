import argparse
import warnings

import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from torchvision import datasets
from tqdm import tqdm

import utils
from model import Net

warnings.filterwarnings("ignore")


def get_shuffle_idx(batch_size):
    """shuffle index for ShuffleBN """
    shuffle_value = torch.randperm(batch_size).long()
    reverse_idx = torch.zeros(batch_size).long()
    arrange_index = torch.arange(batch_size).long()
    reverse_idx.index_copy_(0, shuffle_value, arrange_index)
    return shuffle_value, reverse_idx


def train(model_q, model_k, train_loader, optimizer, epoch, temp=0.07):
    model_q.train()
    queue = torch.zeros((0, features_dim), dtype=torch.float).to('cuda')
    total_loss, n_data, train_bar = 0, 0, tqdm(train_loader)
    for data, target in train_bar:
        x_q, x_k = data
        K, N = queue.shape[0], x_q.shape[0]

        # shuffle BN
        shuffle_idx, reverse_idx = get_shuffle_idx(N)
        x_k = x_k.to('cuda')[shuffle_idx.to('cuda')]
        k = model_k(x_k)
        k = k[reverse_idx.to('cuda')].detach()

        if K >= dictionary_size:
            q = model_q(x_q.to('cuda'))

            l_pos = torch.bmm(q.view(N, 1, -1), k.view(N, -1, 1))
            l_neg = torch.mm(q.view(N, -1), queue.detach().t().view(-1, K).contiguous())

            logits = torch.cat([l_pos.view(N, 1), l_neg], dim=1)
            labels = torch.zeros(N, dtype=torch.long).to('cuda')
            loss = cross_entropy_loss(logits / temp, labels)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            n_data += N
            total_loss += loss.item() * N

            utils.momentum_update(model_q, model_k)
            train_bar.set_description('Train Epoch: [{}/{}] Loss: {:.6f}'.format(epoch, epochs, total_loss / n_data))

        queue = utils.queue_data(queue, k)
        queue = utils.dequeue_data(queue, dictionary_size)

    return total_loss / n_data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train MoCo')
    parser.add_argument('--data_path', type=str, default='/home/data/imagenet/ILSVRC2012', help='Path to dataset')
    parser.add_argument('--model_type', default='resnet18', type=str,
                        choices=['resnet18', 'resnet50'], help='Backbone type')
    parser.add_argument('--batch_size', type=int, default=256, help='Number of images in each mini-batch')
    parser.add_argument('--epochs', type=int, default=200, help='Number of sweeps over the dataset to train')
    parser.add_argument('--features_dim', type=int, default=128, help='Dim of features for each image')
    parser.add_argument('--dictionary_size', type=int, default=65536, help='Size of dictionary')

    args = parser.parse_args()
    batch_size, epochs, features_dim, data_path = args.batch_size, args.epochs, args.features_dim, args.data_path
    dictionary_size, model_type = args.dictionary_size, args.model_type
    train_data = datasets.ImageFolder(root='{}/{}'.format(data_path, 'train'), transform=utils.train_transform)
    train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True, num_workers=8, drop_last=True)

    model_q = Net(model_type, features_dim).to('cuda')
    model_k = Net(model_type, features_dim).to('cuda')
    utils.momentum_update(model_q, model_k, beta=0.0)
    optimizer = optim.SGD(model_q.parameters(), lr=0.03, momentum=0.9, weight_decay=0.0001)
    print("# trainable parameters:", sum(param.numel() if param.requires_grad else 0 for param in model_q.parameters()))
    lr_scheduler = MultiStepLR(optimizer, milestones=[int(epochs * 0.6), int(epochs * 0.8)], gamma=0.1)
    cross_entropy_loss = nn.CrossEntropyLoss()
    results = {'train_loss': []}

    min_loss = float('inf')
    for epoch in range(1, epochs + 1):
        current_loss = train(model_q, model_k, train_loader, optimizer, epoch)
        results['train_loss'].append(current_loss)
        # save statistics
        data_frame = pd.DataFrame(data=results, index=range(1, epoch + 1))
        data_frame.to_csv('results/features_extractor_{}_{}_{}_results.csv'
                          .format(model_type, features_dim, dictionary_size), index_label='epoch')
        lr_scheduler.step(epoch)
        if current_loss < min_loss:
            min_loss = current_loss
            torch.save(model_q.state_dict(), 'epochs/features_extractor_{}_{}_{}.pth'
                       .format(model_type, features_dim, dictionary_size))
