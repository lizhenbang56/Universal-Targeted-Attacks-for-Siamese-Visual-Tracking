# -*- coding: utf-8 -*
import random
import copy
from collections import OrderedDict
import os
from loguru import logger
from tqdm import tqdm
import torch
from torch import nn
from torch.utils.tensorboard import SummaryWriter
from videoanalyst.utils import (Timer, move_data_to_device)
from videoanalyst.utils.visualize_training import visualize_training, visualize_patched_img
from ..trainer_base import TRACK_TRAINERS, TrainerBase


def fgsm_attack(uap, data_grad, epsilon):
    sign_data_grad = data_grad.sgn()
    new_uap = uap - epsilon*sign_data_grad
    return new_uap


def get_patch_grad(tensor, pos, patch_w, patch_h):
    grad = torch.zeros((1, 3, patch_h,patch_w), device=tensor.device)
    for t, p in zip(tensor, pos):
        x1, y1, x2, y2 = p
        grad += t[:, y1:y2, x1:x2]
    return grad / tensor.shape[0]


def filter(tensor, filter):
    fft = torch.fft.fft2(tensor)  # 把扰动图像转到频域
    fft_center = torch.fft.fftshift(fft)  # 把低频移到中央
    fft_center_mask = filter * fft_center  # 滤波
    ifftshift = torch.fft.ifftshift(fft_center_mask)  # 进行反平移
    ifft = torch.fft.ifft2(ifftshift)  # 变回图像
    return ifft


def restrict_tensor(data, do=False):
    if do:
        # return 1.0 * data/(torch.max(torch.abs(data)))
        # return data/10
        return torch.clip(data, -10, 10)
    else:
        return data


def generate_perturbation_x1(patch_x, filter_x, patch_x_background, color_channel, dtype, x1, y1, x2, y2, requires_grad=False):
    ifft = filter(patch_x[0,color_channel,:,:], filter_x)  # 进行高通滤波
    perturbed_x_one_channel_mask = restrict_tensor(ifft.to(dtype))  # 限制值，转为整型
    if not requires_grad:
        patch_x_background[0][color_channel, y1:y2+1, x1:x2+1] = perturbed_x_one_channel_mask  # 前景模板施加到背景模板上
    return patch_x_background[0][color_channel, :, :]


def generate_perturbation_x(patch_x, filter_x, patch_x_background, color_channel, dtype, x1, y1, x2, y2, requires_grad=False):
    ifft = filter(patch_x[0,color_channel,:,:], filter_x)  # 进行高通滤波
    perturbed_x_one_channel_mask = restrict_tensor(ifft.to(dtype))  # 限制值，转为整型
    return perturbed_x_one_channel_mask


def apply_perturbation(search_img, perturbation, x1, y1, x2, y2, patch_x_background, color_channel):
    search_img[y1:y2+1, x1:x2+1] += perturbation
    search_img += restrict_tensor(patch_x_background[0][color_channel, :, :], True)
    return search_img


@TRACK_TRAINERS.register
class RegularTrainer(TrainerBase):
    r"""
    Trainer to test the vot dataset, the result is saved as follows
    exp_dir/logs/$dataset_name$/$tracker_name$/baseline
                                    |-$video_name$/ floder of result files
                                    |-eval_result.csv evaluation result file

    Hyper-parameters
    ----------------
    devices: List[str]
        list of string
    """
    extra_hyper_params = dict(
        minibatch=1,
        nr_image_per_epoch=1,
        max_epoch=1,
        snapshot="",
    )

    def __init__(self, optimizer, dataloader, dataloader_uap, monitors=[]):
        r"""
        Crete tester with config and pipeline

        Arguments
        ---------
        optimizer: ModuleBase
            including optimizer, model and loss
        dataloder: DataLoader
            PyTorch dataloader object. 
            Usage: batch_data = next(dataloader)
        """
        super(RegularTrainer, self).__init__(optimizer, dataloader, dataloader_uap, monitors)
        # update state
        self._state["epoch"] = -1  # uninitialized
        self._state["initialized"] = False
        self._state["devices"] = torch.device("cuda:0")

    def init_train(self, ):
        torch.cuda.empty_cache()
        # move model & loss to target devices
        devs = self._state["devices"]
        self._model.train()
        # load from self._state["snapshot_file"]
        self.load_snapshot()
        # parallelism with Data Parallel (DP)
        if len(self._state["devices"]) > 1:
            self._model = nn.DataParallel(self._model, device_ids=devs)
            logger.info("Use nn.DataParallel for data parallelism")
        super(RegularTrainer, self).init_train()
        logger.info("{} initialized".format(type(self).__name__))

    def train(self, patch_x, patch_x_background, uap_z, real_iter_num, signal_img_debug, visualize, optimizer, dataset_name, params):
        """"""
        if not self._state["initialized"]:
            self.init_train()
        self._state["initialized"] = True

        """START：设定参数"""
        self.cls_weight = params['cls_weight']
        self.ctr_weight = params['ctr_weight']
        self.reg_weight = params['reg_weight']

        # 设定搜索图像损失权重与学习率
        self.l2_x_background_weight = 0.001  # 搜索图像的l2权重同样要大。因为希望x扰动小。
        self.lr_x = 1.0  # 修改成和z一样
        self.optimize_mode = 'FGSM'
        """END：设定参数"""

        """设定保存路径"""
        if params['phase'] == 'OURS':
            if self.cls_weight == 1.0 and self.ctr_weight == 1.0 and self.reg_weight == 1.0:
                self.save_name = str(params['patch_size'])
            elif self.cls_weight == 1.0 and self.ctr_weight == 0.0 and self.reg_weight == 0.0:
                self.save_name = str(params['patch_size']) + '_ctr100'
            elif self.cls_weight == 0.0 and self.ctr_weight == 1.0 and self.reg_weight == 0.0:
                self.save_name = str(params['patch_size']) + '_ctr010'
            elif self.cls_weight == 0.0 and self.ctr_weight == 0.0 and self.reg_weight == 1.0:
                self.save_name = str(params['patch_size']) + '_ctr001'
            else:
                assert False, self
        else:
            self.save_name = params['phase']
        """设定保存路径"""

        """START：设置保存路径"""
        print(self.save_name)
        save_dir = os.path.join('snapshots_imperceptible_patch', self.save_name)
        if signal_img_debug:
            save_dir = '/tmp/uap_debug'
        self.writer = SummaryWriter(save_dir)
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        """END：设置保存路径"""

        self._state["epoch"] += 1
        epoch = self._state["epoch"]
        num_iterations = self._hyper_params["num_iterations"]

        # udpate engine_state
        self._state["max_epoch"] = self._hyper_params["max_epoch"]
        self._state["max_iteration"] = num_iterations

        self._optimizer.modify_grad(epoch)
        pbar = tqdm(range(num_iterations))
        self._state["pbar"] = pbar
        self._state["print_str"] = ""

        time_dict = OrderedDict()
        for iteration, _ in enumerate(pbar):
            real_iter_num += 1
            self._state["iteration"] = iteration
            with Timer(name="data", output_dict=time_dict):

                """START：获取 training data"""
                training_data = next(self._dataloader)
                background = True
                """END：获取 training data"""

            training_data = move_data_to_device(training_data,
                                                self._state["devices"][0])

            schedule_info = self._optimizer.schedule(epoch, iteration)
            self._optimizer.zero_grad()

            # forward propagation
            with Timer(name="fwd", output_dict=time_dict):

                """START：将扰动放至正确的显卡"""
                patch_x = patch_x.to(self._state["devices"][0])
                patch_x_background = patch_x_background.to(self._state["devices"][0])
                uap_z = uap_z.to(self._state["devices"][0])
                filter_x = params['filter_x'].to(self._state["devices"][0])
                filter_z = params['filter_z'].to(self._state["devices"][0])
                """END：将扰动放至正确的显卡"""

                """START：设置扰动为可获取梯度"""
                uap_z.requires_grad = False
                patch_x.requires_grad = False
                patch_x_background.requires_grad = True
                """END：设置扰动为可获取梯度"""

                """START：在搜索图像添加补丁"""
                for idx, xyxy in enumerate(training_data['bbox_x']):
                    x1, y1, x2, y2 = [int(var) for var in xyxy]  # 补丁在搜索图像上的位置
                    try:
                        if params['phase'] in ['Ours']:
                            training_data['im_x'][idx, :, y1:y2+1, x1:x2+1] += patch_x[0]  # 不缩放补丁，相加操作，希望不可感知
                        elif params['phase'] == 'AP':
                            training_data['im_x'][idx, :, y1:y2+1, x1:x2+1] = patch_x[0]
                        elif params['phase'] == 'UAP':
                            training_data['im_x'][idx] += patch_x[0]
                        elif params['phase'] == 'FFT':
                            dtype = training_data['im_x'][idx].dtype
                            for color_channel in range(3):
                                perturbed_x_one_channel_mask = generate_perturbation_x(patch_x, filter_x, patch_x_background, color_channel, dtype, x1, y1, x2, y2, background)
                                training_data['im_x'][idx, color_channel, :, :] = apply_perturbation(training_data['im_x'][idx, color_channel, :, :], perturbed_x_one_channel_mask, x1, y1, x2, y2, patch_x_background, color_channel)
                        else:
                            assert False, params['phase']
                    except Exception as e:
                        print("Error paste patch", str(e))
                        continue
                if visualize:
                    visualize_patched_img(training_data['im_x'], name='patched_train_search')
                """END：在搜索图像添加补丁"""

                """START：将扰动叠加至输入图像"""
                if params['phase'] == 'FFT':
                    for color_channel in range(3):
                        ifft = filter(uap_z[0, color_channel, :, :], filter_z)
                        perturbed_z_one_channel_mask = restrict_tensor(ifft.to(dtype))
                        training_data['im_z'][:, color_channel, :, :] = perturbed_z_one_channel_mask.unsqueeze(0) + training_data['im_z'][:, color_channel, :, :].data
                else:
                    training_data['im_z'] = uap_z + training_data['im_z'].data
                """END：将扰动叠加至输入图像"""

                """START：网络前向传播"""
                self._model.eval()  # !!!非常重要。否则造成训练测试不一致。我们根本不训练网络。
                optimizer.zero_grad()
                predict_data = self._model(training_data)
                """END：网络前向传播"""

                """START：可视化训练数据"""
                if visualize:
                    visualize_training(training_data, predict_data)
                """END：可视化训练数据"""

                """START：计算损失"""
                training_losses, extras = OrderedDict(), OrderedDict()
                for loss_name, loss in self._losses.items():
                    training_losses[loss_name], extras[loss_name] = loss(
                        predict_data, training_data)
                norm_x_background_loss = torch.mean(patch_x_background.pow(2))
                norm_z_loss = torch.mean(uap_z.pow(2))
                cls_loss = training_losses['cls']
                ctr_loss = training_losses['ctr']
                reg_loss = training_losses['reg']
                total_loss = self.cls_weight*cls_loss + \
                             self.ctr_weight*ctr_loss + \
                             self.reg_weight*reg_loss + \
                             self.l2_x_background_weight*norm_x_background_loss
                """END：计算损失"""

                """START：模型梯度清空"""
                self._model.zero_grad()
                """END：模型梯度清空"""

                """START：梯度反传"""
                total_loss.backward()
                """END：梯度反传"""

                """START：收集相对于输入图像的梯度"""
                background_grad = torch.mean(patch_x_background.grad.data, dim=0, keepdim=True)
                patch_x_background = fgsm_attack(patch_x_background, background_grad, self.lr_x)  # 对应的gt是真实目标。这里希望预测原理真实目标。
                patch_x_background = patch_x_background.detach()
                """END：收集相对于输入图像的梯度"""

            trainer_data = dict(
                schedule_info=schedule_info,
                training_losses=training_losses,
                extras=extras,
                time_dict=time_dict,
                norm_x_background_loss=norm_x_background_loss.item(),
                norm_z_loss=norm_z_loss.item(),
            )

            """START：记录训练情况"""
            print('cls={:.2f}, ctr={:.2f}, reg={:.2f}, x_background_norm={:.2f}, iou={:.2f}'.format(cls_loss.item(), ctr_loss.item(), reg_loss.item(), norm_x_background_loss.item(), trainer_data['extras']['reg']['iou'].item()))
            """END：记录训练情况"""

            del training_data

            print_str = self._state["print_str"]
            pbar.set_description(print_str)

            """START：保存扰动"""
            if real_iter_num & (real_iter_num - 1) == 0:
                save_path_x_background = os.path.join(save_dir, 'x_background_{}'.format(real_iter_num))
                torch.save(patch_x_background, save_path_x_background)
                print(' save to: {}'.format(save_path_x_background))
            """END：保存扰动"""

        return patch_x, uap_z, real_iter_num


RegularTrainer.default_hyper_params = copy.deepcopy(
    RegularTrainer.default_hyper_params)
RegularTrainer.default_hyper_params.update(RegularTrainer.extra_hyper_params)
