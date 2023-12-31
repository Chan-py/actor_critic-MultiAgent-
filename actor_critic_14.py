from dataclasses import dataclass
from typing import Any, Optional, Union, Dict
import sys
from time import time
import matplotlib.pyplot as plt
from einops import rearrange
import numpy as np
import torch
from torch.multiprocessing import Pool
from torch.distributions.categorical import Categorical
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
from mlagents_envs.environment import UnityEnvironment
from mlagents_envs.base_env import ActionTuple
import cv2
from torchvision import utils
from utils import compute_lambda_returns, LossWithIntermediateLosses
from parallel_env import ParallelEnv
import os
os.environ['KMP_DUPLICATE_LIB_OK']='True'

Batch = Dict[str, torch.Tensor]

@dataclass
class ActorCriticOutput:
    logits_actions0: torch.FloatTensor
    logits_actions1: torch.FloatTensor
    means_values: torch.FloatTensor


@dataclass
class RolloutOutput:
    observations: torch.ByteTensor
    actions0: torch.LongTensor
    actions1: torch.LongTensor
    logits_actions0: torch.FloatTensor
    logits_actions1: torch.FloatTensor
    values: torch.FloatTensor
    rewards: torch.FloatTensor
    ends: torch.BoolTensor


class ActorCritic(nn.Module):
    def __init__(self, act_vocab_size=49) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(3, 32, 3, stride=1, padding=1)
        self.maxp1 = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(32, 32, 3, stride=1, padding=1)
        self.maxp2 = nn.MaxPool2d(2, 2)
        self.conv3 = nn.Conv2d(32, 64, 3, stride=1, padding=1)
        self.maxp3 = nn.MaxPool2d(2, 2)
        self.conv4 = nn.Conv2d(64, 64, 3, stride=1, padding=1)
        self.maxp4 = nn.MaxPool2d(2, 2)

        self.lstm_dim = 512
        self.lstm = nn.LSTMCell(1024, self.lstm_dim)
        self.hx, self.cx = None, None

        self.critic_linear = nn.Linear(512, 1)
        self.actor_linear0 = nn.Linear(512, 7)
        self.actor_linear1 = nn.Linear(512, 7)

    def __repr__(self) -> str:
        return "actor_critic"

    def clear(self) -> None:
        self.hx, self.cx = None, None

    def reset(self, n: int) -> None:
        device = self.conv1.weight.device
        self.hx = torch.zeros(n, self.lstm_dim, device=device)
        self.cx = torch.zeros(n, self.lstm_dim, device=device)

    def forward(self, inputs: torch.FloatTensor) -> ActorCriticOutput:
        assert inputs.ndim == 4 and inputs.shape[1:] == (3, 64, 64)
        assert 0 <= inputs.min() <= 1 and 0 <= inputs.max() <= 1
        x = inputs
        x = x.mul(2).sub(1)
        x = F.relu(self.maxp1(self.conv1(x)))
        x = F.relu(self.maxp2(self.conv2(x)))
        x = F.relu(self.maxp3(self.conv3(x)))
        x = F.relu(self.maxp4(self.conv4(x)))
        x = torch.flatten(x, start_dim=1)

        self.hx, self.cx = self.lstm(x, (self.hx, self.cx))

        logits_actions0 = rearrange(self.actor_linear0(self.hx), 'b a -> b 1 a')
        logits_actions1 = rearrange(self.actor_linear1(self.hx), 'b a -> b 1 a')
        means_values = rearrange(self.critic_linear(self.hx), 'b 1 -> b 1 1')

        return ActorCriticOutput(logits_actions0, logits_actions1, means_values)

    def compute_loss(self, outputs, envs,  gamma: float = 0.99, lambda_: float = 0.95, entropy_weight: float = 0.001, **kwargs: Any) -> LossWithIntermediateLosses:
        rewards_total = np.mean(torch.sum(outputs.rewards, dim=1).detach().cpu().numpy())
        with torch.no_grad():
            lambda_returns = compute_lambda_returns(
                rewards=outputs.rewards,
                values=outputs.values,
                ends=outputs.ends,
                gamma=gamma,
                lambda_=lambda_,
            )[:, :-1]

        values = outputs.values[:, :-1]

        d0 = Categorical(logits=outputs.logits_actions0[:, :-1])
        d1 = Categorical(logits=outputs.logits_actions1[:, :-1])
        log_probs0 = d0.log_prob(outputs.actions0[:, :-1])
        log_probs1 = d1.log_prob(outputs.actions1[:, :-1])
        loss_actions0 = -1 * (log_probs0 * (lambda_returns - values.detach())).mean()
        loss_actions1 = -1 * (log_probs1 * (lambda_returns - values.detach())).mean()
        loss_entropy0 = - entropy_weight * d0.entropy().mean()
        loss_entropy1 = - entropy_weight * d1.entropy().mean()

        loss_values = F.mse_loss(values, lambda_returns)

        return (LossWithIntermediateLosses(loss_actions=loss_actions0, loss_values=loss_values, loss_entropy=loss_entropy0), LossWithIntermediateLosses(loss_actions=loss_actions1, loss_values=loss_values, loss_entropy=loss_entropy1), rewards_total)

   
def batch_rollout(actor0, envs) -> RolloutOutput:
        device = actor0.conv1.weight.device
        n = envs.n

        actor0.reset(n=n)
        #actor1.reset(n=n)
        all_actions0, all_actions1 = [], []
        all_logits_actions0, all_logits_actions1 = [] ,[]
        all_values0, all_values1 = [], []
        all_rewards = []
        all_ends = []
        all_observations0, all_observations1 = [], []

        envs.reset()
        obss0, obss1, _, _ = envs.step(np.zeros((n,2)))
        
        for _ in range(200):
            obss0 = torch.FloatTensor(obss0).to(device)
            #obss1 = torch.FloatTensor(obss1).to(device)
            outputs_ac0 = actor0(obss0)
            #outputs_ac1 = actor1(obss1)
            action_token0 = Categorical(logits=outputs_ac0.logits_actions0).sample()
            action_token1 = Categorical(logits=outputs_ac0.logits_actions1).sample()

            all_observations0.append(obss0)
            all_actions0.append(action_token0)
            all_logits_actions0.append(outputs_ac0.logits_actions0)
            all_values0.append(outputs_ac0.means_values)

            #all_observations1.append(obss1)  
            all_actions1.append(action_token1)
            all_logits_actions1.append(outputs_ac0.logits_actions1)
            # all_values1.append(outputs_ac1.means_values)

            #obss0, obss1, step_rewards, dones = envs.step(np.concatenate((action_token0, action_token1), axis = 1))
            obss0, obss1, step_rewards, dones = envs.step(np.concatenate((action_token0, action_token1), axis = 1))

            all_rewards.append(torch.tensor(step_rewards).reshape(-1, 1))
            all_ends.append(torch.tensor(dones).reshape(-1, 1))


            if np.any(dones):
                assert np.all(dones)
                # envs.reset()
                break
        actor0.clear()
        #actor1.clear()
        return RolloutOutput(
            observations=torch.stack(all_observations0, dim=1).mul(255).byte(),      # (B, T, C, H, W) in [0, 255]
            actions0=torch.cat(all_actions0, dim=1),                                  # (B, T)
            actions1=torch.cat(all_actions1, dim=1),                                  # (B, T)
            logits_actions0=torch.cat(all_logits_actions0, dim=1),                    # (B, T, #actions)
            logits_actions1=torch.cat(all_logits_actions1, dim=1),                    # (B, T, #actions)
            values=rearrange(torch.cat(all_values0, dim=1), 'b t 1 -> b t'),         # (B, T)
            rewards=torch.cat(all_rewards, dim=1).to(device),                       # (B, T)
            ends=torch.cat(all_ends, dim=1).to(device),                             # (B, T)
        )
        #, RolloutOutput(
        #    observations=torch.stack(all_observations1, dim=1).mul(255).byte(),      # (B, T, C, H, W) in [0, 255]
        #    actions=torch.cat(all_actions1, dim=1),                                  # (B, T)
        #    logits_actions=torch.cat(all_logits_actions1, dim=1),                    # (B, T, #actions)
        #    values=rearrange(torch.cat(all_values1, dim=1), 'b t 1 -> b t'),         # (B, T)
        #    rewards=torch.cat(all_rewards, dim=1).to(device),                       # (B, T)
        #    ends=torch.cat(all_ends, dim=1).to(device),                             # (B, T)
        #)
if __name__ == '__main__':
    import pickle

    alg0 = ActorCritic()
    if os.path.exists('alg0.pt'):
        alg0.load_state_dict(torch.load('alg0.pt'))

    optimizer0 = torch.optim.Adam(alg0.parameters(), lr=0.0001)
    #alg1 = ActorCritic()
    #optimizer1 = torch.optim.Adam(alg1.parameters(), lr=0.0001)
    num_envs = 16
    envs = ParallelEnv(num_envs)

    n_epoch = 10000
    max_grad_norm = 10

    # loss_actions_all = []
    # loss_values_all = []
    # loss_entropies = []
    loss_totals = []
    rewards_all = []

    if os.path.exists('losses.pkl'):
        loss_totals, rewards_all = pickle.load(open('losses.pkl', 'rb'))

    for epoch in tqdm(range(n_epoch)):
        #output0, output1 = batch_rollout(alg0, alg1, envs)
        output0 = batch_rollout(alg0, envs)
        optimizer0.zero_grad()
        losses0, losses1, rewards0 = alg0.compute_loss(output0, envs)
        loss_total = losses0.loss_total + losses1.loss_total
        loss_total.backward()
        torch.nn.utils.clip_grad_norm_(alg0.parameters(), max_grad_norm)
        optimizer0.step()

        #optimizer1.zero_grad()
        #losses1, rewards1 = alg1.compute_loss(output1, envs)
        #loss_total_step1 = losses1.loss_total
        #loss_total_step1.backward()    
        #torch.nn.utils.clip_grad_norm_(alg1.parameters(), max_grad_norm)
        #optimizer1.step()

        #loss_totals.append(loss_total_step0.item() + loss_total_step1.item())
        loss_totals.append(loss_total.item())
        # loss_values_all.append(losses["loss_values"].item())
        # loss_entropies.append(losses["loss_entropy"].item())
        # loss_actions_all.append(losses["loss_actions"].item())
        rewards_all.append(rewards0)
        print('###########', epoch, rewards0)
        pickle.dump([loss_totals, rewards_all], open('losses.pkl', 'wb'))

        # pickle.dump([loss_totals, loss_values_all, loss_entropies, loss_actions_all, rewards_all], open('losses.pkl', 'wb'))
        if epoch != 0 and epoch % 10 == 0:
            torch.save(alg0.state_dict(), 'alg0.pt')
            torch.save(optimizer0.state_dict(), 'optimizer0.pt')

            plt.plot(rewards_all)
            plt.savefig('rewards_all.png')
            plt.close()