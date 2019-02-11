from itertools import chain
import torch
from torch.distributions import MultivariateNormal, Bernoulli, Categorical


class HandWritingRNN(torch.nn.Module):
    def __init__(self, memory_cells=400, n_gaussians=20, num_layers=3):
        """
        input_size is fixed to 3.
        hidden_size = memory_cells 
        Output dimension after the fully connected layer = (6 * n_gaussians + 1)
        """
        super().__init__()
        self.n_gaussians = n_gaussians

        self.rnns = torch.nn.ModuleList()
        for i in range(num_layers):
            input_size = 3 if i == 0 else (3 + memory_cells)
            self.rnns.append(torch.nn.LSTM(input_size, memory_cells, 1))

        self.last_layer = torch.nn.Linear(
            in_features=memory_cells * num_layers, out_features=(6 * n_gaussians + 1)
        )

        self.sigmoid = torch.nn.Sigmoid()
        self.softmax = torch.nn.Softmax(dim=2)
        self.tanh = torch.nn.Tanh()

    def forward(self, inp, lstm_in_states=None):
        """
        first_layer of self.rnns gets inp as input
        subsequent layers of self.rnns get inp concatenated with output of
        previous layer as the input. 
        args : 
            inp : input sequence of dimensions (T, B, 3)
            lstm_in_states : input states for num_layers number of lstm layers;
                            it is a list of num_layers tupels (h_i, c_i), with 
                            both h_i and c_i tensor of dimensions (memory_cells,)
        """
        rnn_out = []
        rnn_out.append(
            self.rnns[0](inp, lstm_in_states[0])
            if lstm_in_states != None
            else self.rnns[0](inp)
        )

        for i, rnn in enumerate(self.rnns[1:]):
            rnn_inp = torch.cat((rnn_out[-1][0], inp), dim=2)
            rnn_out.append(
                rnn(rnn_inp, lstm_in_states[i + 1])
                if lstm_in_states != None
                else rnn(rnn_inp)
            )

        # clip LSTM derivatives to (-10, 10)
        if(rnn_out[0][0].requires_grad):
            for o in rnn_out:
                o[0].register_hook(lambda x: x.clamp(-10, 10))  # h_1 to h_n
                # o[1][1].register_hook(lambda x: x.clamp(-10, 10))  # c_n
                # the above clamp works on CPU but not on GPU (need to debug)

        # rnn_out is a list of tuples (out, (h, c))
        lstm_out_states = [o[1] for o in rnn_out]
        rnn_out = torch.cat([o[0] for o in rnn_out], dim=2)

        y = self.last_layer(rnn_out)
        if y.requires_grad:
            y.register_hook(lambda x: x.clamp(min=-100, max=100))

        pi = self.softmax(y[:, :, : self.n_gaussians])
        mu = y[:, :, self.n_gaussians: 3 * self.n_gaussians]
        sigma = torch.exp(y[:, :, 3 * self.n_gaussians: 5 * self.n_gaussians])
        rho = self.tanh(
            y[:, :, 5 * self.n_gaussians: 6 * self.n_gaussians])  # * 0.9
        e = self.sigmoid(y[:, :, 6 * self.n_gaussians])

        return e, pi, mu, sigma, rho, lstm_out_states

    def init_params(self):
        for param in self.rnns.parameters():
            if param.dim() == 1:
                torch.nn.init.uniform_(param, -1e-2, 1e-2)
            else:
                torch.nn.init.orthogonal_(param)
        for param in self.last_layer.parameters():
            if param.dim() == 1:
                torch.nn.init.uniform_(param, -1e-2, 1e-2)
            else:
                torch.nn.init.xavier_uniform_(param)

    def generate(self, length=300, batch=1, device=torch.device("cpu")):
        """
        Get a random sample from the distribution (model)
        """
        samples = torch.zeros(length + 1, batch, 3,
                              device=device)  # batch_first=false
        lstm_states = None

        for i in range(1, length + 1):
            # get distribution parameters
            with torch.no_grad():
                e, pi, mu, sigma, rho, lstm_states = self.forward(
                    samples[i - 1: i], lstm_states
                )
            # sample from the distribution (returned parameters)
            # samples[i, :, 0] = e[-1, :] > 0.5
            distrbn1 = Bernoulli(e[-1, :])
            samples[i, :, 0] = distrbn1.sample()

            # selected_mode = torch.argmax(pi[-1, :, :], dim=1) # shape = (batch,)
            distrbn2 = Categorical(pi[-1, :, :])
            selected_mode = distrbn2.sample()

            index_1 = selected_mode.unsqueeze(1)  # shape (batch, 1)
            # shape (batch, 1, 2)
            index_2 = torch.stack([index_1, index_1], dim=2)

            mu = (
                mu[-1]
                .view(batch, self.n_gaussians, 2)
                .gather(dim=1, index=index_2)
                .squeeze()
            )
            sigma = (
                sigma[-1]
                .view(batch, self.n_gaussians, 2)
                .gather(dim=1, index=index_2)
                .squeeze()
            )
            rho = rho[-1].gather(dim=1, index=index_1).squeeze()

            sigma2d = sigma.new_zeros(batch, 2, 2)
            sigma2d[:, 0, 0] = sigma[:, 0] ** 2
            sigma2d[:, 1, 1] = sigma[:, 1] ** 2
            sigma2d[:, 0, 1] = rho[:] * sigma[:, 0] * sigma[:, 1]
            sigma2d[:, 1, 0] = sigma2d[:, 0, 1]

            distribn = MultivariateNormal(loc=mu, covariance_matrix=sigma2d)

            samples[i, :, 1:] = distribn.sample()

        return samples[1:, :, :]  # remove dummy first zeros


# ------------------------------------------------------------------------------


class HandWritingSynthRNN(torch.nn.Module):
    def __init__(
        self,
        memory_cells=400,
        n_gaussians=20,
        num_layers=3,
        n_gaussians_window=10,
        n_char=57,
        max_stroke_len=1000,
        max_sentence_len=59,
    ):
        """
        input_size is fixed to 3.
        hidden_size = memory_cells 
        Output dimension after the fully connected layer = (6 * n_gaussians + 1)
        """
        super().__init__()
        self.n_gaussians = n_gaussians
        self.n_gaussians_window = n_gaussians_window
        self.memory_cells = memory_cells
        self.n_char = n_char

        self.first_rnn = torch.nn.LSTMCell(3 + n_char, memory_cells)
        self.rnns = torch.nn.ModuleList()
        input_size = 3 + memory_cells + n_char
        for i in range(num_layers - 1):
            self.rnns.append(torch.nn.LSTM(input_size, memory_cells, 1))

        self.h_to_w = torch.nn.Linear(
            in_features=memory_cells, out_features=3 * n_gaussians_window
        )
        # n_gaussians_window number of alpha, beta and kappa each

        self.last_layer = torch.nn.Linear(
            in_features=memory_cells * num_layers, out_features=(6 * n_gaussians + 1)
        )

        self.sigmoid = torch.nn.Sigmoid()
        self.softmax = torch.nn.Softmax(dim=2)
        self.tanh = torch.nn.Tanh()

    def forward(self, inp, c_seq, lstm_in_states=None, prev_window=None, prev_kappa=0):
        """
        first_layer of self.rnns gets inp as input
        subsequent layers of self.rnns get inp concatenated with output of
        previous layer as the input. 
        args: 
            inp: input sequence of dimensions (T, B, 3)
            c_seq: one-hot encoded and padded char sequence of 
                dimension (B, U, n_char)
            lstm_in_states: input states for num_layers number of lstm 
                layers; it is a list of num_layers tupels (h_i, c_i), with
                both h_i and c_i tensor of dimensions (memory_cells,)
            prev_window: (B, n_char)
            prev_kappa: (B, K=10, 1)
        """

        if prev_window is None:
            prev_window = inp.new_zeros(
                inp.shape[1], c_seq.shape[-1])  # (B, n_char)

        window_list = []
        first_rnn_out = []
        h, c = (
            [inp.new_zeros(inp.shape[1], self.memory_cells)] * 2
            if lstm_in_states is None
            else lstm_in_states[0]
        )
        for x in inp:
            rnn_inp = torch.cat((x, prev_window), dim=1)  # (B, 3+n_char)
            h, c = self.first_rnn(rnn_inp, (h, c))

            # clip LSTM derivatives to (-10, 10)
            if(h.requires_grad):
                h.register_hook(lambda x: x.clamp(-10, 10))
                # c.register_hook(lambda x: x.clamp(-10, 10))
                # the above clamp works on CPU but not on GPU (need to debug)

            first_rnn_out.append(h)
            # Paramters for soft-window calculation
            window_params = self.h_to_w(h).exp()  # (B, 3*K)
            alpha, beta, kappa = window_params.unsqueeze(
                -1).chunk(chunks=3, dim=1)
            # shape : (B, K=10, 1); unsqueeze() for broadcasting into (B, K, U)
            kappa += prev_kappa
            beta = -beta
            # Weights for soft-window calculation
            U = c_seq.shape[1]
            u_seq = torch.arange(1, U + 1).float().to(x.device)  # shape : (U)
            phi = ((beta * (kappa - u_seq) ** 2).exp()
                   * alpha).sum(dim=1)  # (B, U)

            # shape: (B, n_char)
            prev_window = (phi.unsqueeze(2) * c_seq).sum(dim=1)
            if prev_window.requires_grad:
                prev_window.register_hook(lambda x: x.clamp(-100, 100))
            window_list.append(prev_window)
            prev_kappa = kappa

        # save the output and states of first_rnn (LSTMCell module) in
        # the format of returned value of an LSTM module
        # [(T, B, memory_cell)]
        rnn_out = [(torch.stack(first_rnn_out, dim=0), (h, c))]
        window = torch.stack(window_list, dim=0)  # (T, B, memory_cell)

        # Running rest of the rnn layers
        for i, rnn in enumerate(self.rnns):
            rnn_inp = torch.cat((rnn_out[-1][0], inp, window), dim=2)
            rnn_out.append(
                rnn(rnn_inp, lstm_in_states[i + 1])
                if lstm_in_states != None
                else rnn(rnn_inp)
            )

        # clip LSTM derivatives to (-10, 10)
        if(rnn_out[1][0].requires_grad):
            for o in rnn_out[1:]:
                o[0].register_hook(lambda x: x.clamp(-10, 10))  # h_1 to h_n
                # o[1][1].register_hook(lambda x: x.clamp(-10, 10))  # c_n
                # the above clamp works on CPU but not on GPU (need to debug)

        # rnn_out is a list of tuples (out, (h, c))
        lstm_out_states = [o[1] for o in rnn_out]
        rnn_out = torch.cat([o[0] for o in rnn_out], dim=2)
        y = self.last_layer(rnn_out)

        if y.requires_grad:
            y.register_hook(lambda x: x.clamp(min=-100, max=100))

        pi = self.softmax(y[:, :, : self.n_gaussians])
        mu = y[:, :, self.n_gaussians: 3 * self.n_gaussians]
        sigma = torch.exp(y[:, :, 3 * self.n_gaussians: 5 * self.n_gaussians])
        # sigma = y[:, :, 3*self.n_gaussians:5*self.n_gaussians]
        rho = self.tanh(
            y[:, :, 5 * self.n_gaussians: 6 * self.n_gaussians])  # * 0.9
        e = self.sigmoid(y[:, :, 6 * self.n_gaussians])

        return e, pi, mu, sigma, rho, lstm_out_states, prev_window, prev_kappa

    def generate(self, sentences, device=torch.device("cpu")):
        """
        Get handwritten form for given sentences
        arguments:
            sentences: List of one-hot encoded sentences (without padding)
        return:
            samples: tensor of handwritten form for the sentences
        """
        c_seq = torch.nn.utils.rnn.pad_sequence(
            sentences, batch_first=True, padding_value=0.0
        )
        batch, U, n_char = c_seq.shape
        length = 600  # this needs to change (length should not be hard-coded)
        # empty matrix of required shape with batch_first = False
        samples = torch.empty(length + 1, batch, 3, device=device)
        lstm_states = None
        window = torch.zeros(batch, n_char, device=device)
        kappa = torch.zeros(batch, self.n_gaussians_window, 1, device=device)

        for i in range(1, length + 1):
            # get distribution parameters
            with torch.no_grad():
                e, pi, mu, sigma, rho, lstm_states, window, kappa = self.forward(
                    samples[i - 1: i], c_seq, lstm_states, window, kappa
                )
            # sample from the distribution (returned parameters)
            # samples[i, :, 0] = e[-1, :] > 0.5
            distrbn1 = Bernoulli(e[-1, :])
            samples[i, :, 0] = distrbn1.sample()

            # selected_mode = torch.argmax(pi[-1, :, :], dim=1) # shape = (batch,)
            distrbn2 = Categorical(pi[-1, :, :])
            selected_mode = distrbn2.sample()

            index_1 = selected_mode.unsqueeze(1)  # shape (batch, 1)
            # shape (batch, 1, 2)
            index_2 = torch.stack([index_1, index_1], dim=2)

            mu = (
                mu[-1]
                .view(batch, self.n_gaussians, 2)
                .gather(dim=1, index=index_2)
                .squeeze()
            )
            sigma = (
                sigma[-1]
                .view(batch, self.n_gaussians, 2)
                .gather(dim=1, index=index_2)
                .squeeze()
            )
            rho = rho[-1].gather(dim=1, index=index_1).squeeze()

            sigma2d = sigma.new_zeros(batch, 2, 2)
            sigma2d[:, 0, 0] = sigma[:, 0] ** 2
            sigma2d[:, 1, 1] = sigma[:, 1] ** 2
            sigma2d[:, 0, 1] = rho[:] * sigma[:, 0] * sigma[:, 1]
            sigma2d[:, 1, 0] = sigma2d[:, 0, 1]

            distribn = MultivariateNormal(loc=mu, covariance_matrix=sigma2d)

            samples[i, :, 1:] = distribn.sample()

        return samples[1:, :, :]  # remove dummy first zeros

    def init_params(self):
        for param in chain(self.first_rnn.parameters(), self.rnns.parameters()):
            if param.dim() == 1:
                torch.nn.init.uniform_(param, -1e-2, 1e-2)
            else:
                torch.nn.init.orthogonal_(param)
        for param in chain(self.last_layer.parameters(), self.h_to_w.parameters()):
            if param.dim() == 1:
                torch.nn.init.uniform_(param, -1e-2, 1e-2)
            else:
                torch.nn.init.xavier_uniform_(param)
