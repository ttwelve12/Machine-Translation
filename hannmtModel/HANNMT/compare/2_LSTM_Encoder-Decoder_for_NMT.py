import torch
import torch.nn as nn
import torch.optim as optim
import torchtext

import spacy
import numpy as np
import random
import math
import time
import os

os.environ['CUDA_VISIBLE_DEVICES'] = '0,1,2,3'

''' 1. Prepare Data '''

SEED = 1234

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
torch.cuda.manual_seed(SEED)
torch.backends.cudnn.deterministic = True

# src:en
spacy_en = spacy.load('en')
# trg:zh
spacy_zh = spacy.load('zh')

# tokenize
def tokenize_en(text):
    """
    Tokenizes English text from a string into a list of strings (tokens) and reverses it
    """
    return [tok.text for tok in spacy_en.tokenizer(text)][::-1]

def tokenize_zh(text):
    """
    Tokenizes Chinese text from a string into a list of strings (tokens)
    """
    return [tok.text for tok in spacy_zh.tokenizer(text)]


SRC = torchtext.data.Field(tokenize = tokenize_en,
                           init_token = '<sos>',
                           eos_token = '<eos>',
                           lower = True)

TRG = torchtext.data.Field(tokenize = tokenize_zh,
                           init_token = '<sos>',
                           eos_token = '<eos>',
                           lower = True)

train_data, valid_data, test_data = torchtext.data.TabularDataset.splits(
            path='./data/',
            train='train.tsv',
            validation='valid.tsv',
            test='test.tsv',
            format='tsv',
            fields=[('src', SRC), ('trg', TRG)])

print("Number of training examples: " + str(len(train_data.examples)))
print("Number of validation examples: " + str(len(valid_data.examples)))
print("Number of testing examples: " + str(len(test_data.examples)))

SRC.build_vocab(train_data, min_freq = 2)
TRG.build_vocab(train_data, min_freq = 2)

print("Unique tokens in source (en) vocabulary: " + str(len(SRC.vocab)))
print("Unique tokens in target (zh) vocabulary: " + str(len(TRG.vocab)))

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

BATCH_SIZE = 16

train_iterator, valid_iterator, test_iterator = torchtext.data.BucketIterator.splits(
                                                (train_data, valid_data, test_data),
                                                batch_size = BATCH_SIZE,
                                                device = device,
                                                sort = False)

''' 2. Building the Seq2Seq Model '''

class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout):
        super().__init__()

        self.hid_dim = hid_dim
        self.n_layers = n_layers

        self.embedding = nn.Embedding(input_dim, emb_dim)

        self.rnn = nn.LSTM(emb_dim, hid_dim, n_layers, dropout = dropout)

        self.dropout = nn.Dropout(dropout)

    def forward(self, src):

        #src = [src len, batch size]

        embedded = self.dropout(self.embedding(src))

        #embedded = [src len, batch size, emb dim]

        outputs, (hidden, cell) = self.rnn(embedded)

        #outputs = [src len, batch size, hid dim * n directions]
        #hidden = [n layers * n directions, batch size, hid dim]
        #cell = [n layers * n directions, batch size, hid dim]

        #outputs are always from the top hidden layer

        return hidden, cell


class Decoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers, dropout):
        super().__init__()

        self.output_dim = output_dim
        self.hid_dim = hid_dim
        self.n_layers = n_layers

        self.embedding = nn.Embedding(output_dim, emb_dim)

        self.rnn = nn.LSTM(emb_dim, hid_dim, n_layers, dropout = dropout)

        self.fc_out = nn.Linear(hid_dim, output_dim)

        self.dropout = nn.Dropout(dropout)

    def forward(self, input, hidden, cell):

        #input = [batch size]
        #hidden = [n layers * n directions, batch size, hid dim]
        #cell = [n layers * n directions, batch size, hid dim]

        #n directions in the decoder will both always be 1, therefore:
        #hidden = [n layers, batch size, hid dim]
        #context = [n layers, batch size, hid dim]

        input = input.unsqueeze(0)

        #input = [1, batch size]

        embedded = self.dropout(self.embedding(input))

        #embedded = [1, batch size, emb dim]

        output, (hidden, cell) = self.rnn(embedded, (hidden, cell))

        #output = [seq len, batch size, hid dim * n directions]
        #hidden = [n layers * n directions, batch size, hid dim]
        #cell = [n layers * n directions, batch size, hid dim]

        #seq len and n directions will always be 1 in the decoder, therefore:
        #output = [1, batch size, hid dim]
        #hidden = [n layers, batch size, hid dim]
        #cell = [n layers, batch size, hid dim]

        prediction = self.fc_out(output.squeeze(0))

        #prediction = [batch size, output dim]

        return prediction, hidden, cell


class Seq2Seq(nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device

        assert encoder.hid_dim == decoder.hid_dim, \
            "Hidden dimensions of encoder and decoder must be equal!"
        assert encoder.n_layers == decoder.n_layers, \
            "Encoder and decoder must have equal number of layers!"

    def forward(self, src, trg, teacher_forcing_ratio = 0.5):

        #src = [src len, batch size]
        #trg = [trg len, batch size]
        #teacher_forcing_ratio is probability to use teacher forcing
        #e.g. if teacher_forcing_ratio is 0.75 we use ground-truth inputs 75% of the time

        batch_size = trg.shape[1]
        trg_len = trg.shape[0]
        trg_vocab_size = self.decoder.output_dim

        #tensor to store decoder outputs
        outputs = torch.zeros(trg_len, batch_size, trg_vocab_size).to(self.device)

        #last hidden state of the encoder is used as the initial hidden state of the decoder
        hidden, cell = self.encoder(src)

        #first input to the decoder is the <sos> tokens
        input = trg[0,:]

        for t in range(1, trg_len):

            #insert input token embedding, previous hidden and previous cell states
            #receive output tensor (predictions) and new hidden and cell states
            output, hidden, cell = self.decoder(input, hidden, cell)

            #place predictions in a tensor holding predictions for each token
            outputs[t] = output

            #decide if we are going to use teacher forcing or not
            teacher_force = random.random() < teacher_forcing_ratio

            #get the highest predicted token from our predictions
            top1 = output.argmax(1)

            #if teacher forcing, use actual next token as next input
            #if not, use predicted token
            input = trg[t] if teacher_force else top1

        return outputs


''' 3. Training the Seq2Seq Model '''

INPUT_DIM = len(SRC.vocab)
OUTPUT_DIM = len(TRG.vocab)
ENC_EMB_DIM = 256
DEC_EMB_DIM = 256
HID_DIM = 512
N_LAYERS = 2
ENC_DROPOUT = 0.5
DEC_DROPOUT = 0.5

enc = Encoder(INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT)
dec = Decoder(OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT)

model = Seq2Seq(enc, dec, device)

if torch.cuda.device_count() > 1:
    model = nn.DataParallel(model, device_ids=[0, 1, 2, 3]) # consistent with cuda

model = model.to(device)


def init_weights(m):
    for name, param in m.named_parameters():
        nn.init.uniform_(param.data, -0.08, 0.08)


model.apply(init_weights)

def count_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


print('The model has ' + str(count_parameters(model)) + ' trainable parameters')

optimizer = optim.Adam(model.parameters())

TRG_PAD_IDX = TRG.vocab.stoi[TRG.pad_token]

criterion = nn.CrossEntropyLoss(ignore_index = TRG_PAD_IDX)


def train(model, iterator, optimizer, criterion, clip):

    model.train()

    epoch_loss = 0

    for i, batch in enumerate(iterator):

        src = batch.src
        trg = batch.trg

        optimizer.zero_grad()

        output = model(src, trg)

        #trg = [trg len, batch size]
        #output = [trg len, batch size, output dim]

        output_dim = output.shape[-1]

        output = output[1:].view(-1, output_dim)
        trg = trg[1:].view(-1)

        #trg = [(trg len - 1) * batch size]
        #output = [(trg len - 1) * batch size, output dim]

        loss = criterion(output, trg)

        loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()

        epoch_loss += loss.item()

    return epoch_loss / len(iterator)


def evaluate(model, iterator, criterion):

    model.eval()

    epoch_loss = 0

    with torch.no_grad():

        for i, batch in enumerate(iterator):

            src = batch.src
            trg = batch.trg

            output = model(src, trg, 0) #turn off teacher forcing

            #trg = [trg len, batch size]
            #output = [trg len, batch size, output dim]

            output_dim = output.shape[-1]

            output = output[1:].view(-1, output_dim)
            trg = trg[1:].view(-1)

            #trg = [(trg len - 1) * batch size]
            #output = [(trg len - 1) * batch size, output dim]

            loss = criterion(output, trg)

            epoch_loss += loss.item()

    return epoch_loss / len(iterator)


def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs


N_EPOCHS = 10
CLIP = 1

for epoch in range(N_EPOCHS):
    savedFiles = os.listdir('./savedModel/1')
    best_valid_loss = float(savedFiles[0][8:-3]) if savedFiles else float('inf')
    if savedFiles:
        model.load_state_dict(torch.load('./savedModel/1/' + savedFiles[0]))

    start_time = time.time()

    train_loss = train(model, train_iterator, optimizer, criterion, CLIP)
    valid_loss = evaluate(model, valid_iterator, criterion)

    end_time = time.time()

    epoch_mins, epoch_secs = epoch_time(start_time, end_time)

    if valid_loss < best_valid_loss:
        best_valid_loss = valid_loss
        savedFiles = os.listdir('./savedModel/1')
        torch.save(model.state_dict(), './savedModel/1/1-model_%f.pt' % valid_loss)
        if savedFiles:
            os.remove('./savedModel/1/' + savedFiles[0])

    print('Epoch: %d | Time: %d m %d s' % (epoch + 1, epoch_mins, epoch_secs))
    print('\tTrain Loss: %f | Train PPL: %f' % (train_loss, math.exp(train_loss)))
    print('\t Valid Loss: %f |  Valid PPL: %f' % (valid_loss, math.exp(valid_loss)))

savedFiles = os.listdir('./savedModel/1')
model.load_state_dict(torch.load('./savedModel/1/' + savedFiles[0]))

test_loss = evaluate(model, test_iterator, criterion)

print('| Test Loss: %f | Test PPL: %f |' % (test_loss, math.exp(test_loss)))
