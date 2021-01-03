import os
import ast
import glob
import numpy as np

from videoanalyst.evaluation.got_benchmark.utils.metrics import rect_iou
from videoanalyst.evaluation.got_benchmark.datasets import GOT10k
from videoanalyst.evaluation.got_benchmark.experiments.got10k import ExperimentGOT10k
from videoanalyst.evaluation.got_benchmark.experiments.otb import ExperimentOTB


def eval_got10k_val():
    """"""
    fgt_paths = sorted(glob.glob(os.path.join(FGT_root, "*.txt")))
    pred_paths = sorted(glob.glob(os.path.join(result_root, '*/*_001.txt')))

    seq_names = dataset.seq_names
    covers = {s: dataset[s][2]['cover'][1:] for s in seq_names}

    fgt_ious = {}
    gt_ious = {}
    times = {}
    for s, data in enumerate(zip(pred_paths, fgt_paths, dataset)):
        pred_path, fgt_path, (_, gt_xywh, meta) = data
        pred_xywh = np.loadtxt(pred_path, delimiter=',')
        fgt_xywh = np.loadtxt(fgt_path, delimiter=',')
        assert pred_xywh.shape == fgt_xywh.shape == gt_xywh.shape
        seq_name = experimentGOT10k.dataset.seq_names[s]
        bound = ast.literal_eval(meta['resolution'])

        fgt_seq_ious = [rect_iou(pred_xywh[1:], fgt_xywh[1:], bound=bound)]
        fgt_seq_ious = [t[covers[seq_name] > 0] for t in fgt_seq_ious]
        fgt_seq_ious = np.concatenate(fgt_seq_ious)
        fgt_ious[seq_name] = fgt_seq_ious

        gt_seq_ious = [rect_iou(pred_xywh[1:], gt_xywh[1:], bound=bound)]
        gt_seq_ious = [t[covers[seq_name] > 0] for t in gt_seq_ious]
        gt_seq_ious = np.concatenate(gt_seq_ious)
        gt_ious[seq_name] = gt_seq_ious

        """START：计算时间"""
        time_file = os.path.join(result_root, seq_name, '%s_time.txt' % seq_name)
        if os.path.exists(time_file):
            seq_times = np.loadtxt(time_file, delimiter=',')
            seq_times = seq_times[~np.isnan(seq_times)]
            seq_times = seq_times[seq_times > 0]
            if len(seq_times) > 0:
                times[seq_name] = seq_times
        """END：计算时间"""
    fgt_ious = np.concatenate(list(fgt_ious.values()))
    gt_ious = np.concatenate(list(gt_ious.values()))
    times = np.concatenate(list(times.values()))
    fgt_ao, fgt_sr, fgt_speed, fgt_succ_curve = experimentGOT10k._evaluate(fgt_ious, times)
    gt_ao, gt_sr, gt_speed, gt_succ_curve = experimentGOT10k._evaluate(gt_ious, times)
    print('FGT_AO={:.3f}\tGT_AO={:.3f}'.format(fgt_ao, gt_ao))
    return


def eval_otb_2015(false_ground_truth):
    experiment = ExperimentOTB('/home/etvuz/projects/adversarial_attack/video_analyst/datasets/OTB/OTB2015',
                               version=2015,
                               result_dir=os.path.join(root, 'video_analyst/logs/GOT-Benchmark/result'),
                               FGT=false_ground_truth)
    eval_result = experiment.report(['siamfcpp_googlenet'])['siamfcpp_googlenet']['overall']
    if false_ground_truth:
        phase = 'FGT'
    else:
        phase = 'GT'
    print('{} Success={:.3f}, Precision={:.3f}, {} FPS'.format(phase, eval_result['success_score'],
                                                            eval_result['precision_score'],
                                                            int(eval_result['speed_fps'])))


if __name__ == '__main__':
    dataset_name = 'OTB_2015'
    root = '/home/etvuz/projects/adversarial_attack'
    if dataset_name == 'OTB_2015':
        result_root = os.path.join(root, 'video_analyst/logs/GOT-Benchmark/result/otb2015/siamfcpp_googlenet')
        eval_otb_2015(false_ground_truth=True)
        eval_otb_2015(false_ground_truth=False)
    elif dataset_name == 'GOT-10k_Val':
        result_root = os.path.join(
            root,
            'video_analyst/snapshots/train_set=fulldata_FGSM_cls=1_ctr=1_reg=1_l2_z=0.005_l2_x=1e-05_lr_z=0.1_lr_x=0.5/'
            'result/32768')
        dataset = GOT10k(os.path.join(root, 'video_analyst/datasets/GOT-10k'), subset='val', return_meta=True)
        experimentGOT10k = ExperimentGOT10k(os.path.join(root, 'video_analyst/datasets/GOT-10k'), subset='val')
        FGT_root = os.path.join(root, 'patch_anno', dataset_name)
        eval_got10k_val()
    else:
        assert False, dataset_name

