import os
import pathlib
import random
import time
import pickle

from torch.utils.tensorboard import SummaryWriter
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torch.utils.data.distributed

from utils.conv_type import FixedSubnetConv, SampleSubnetConv
from utils.logging import AverageMeter, ProgressMeter
from utils.net_utils import (
    set_model_prune_rate,
    bn_weight_init,
    freeze_model_weights,
    save_checkpoint,
    get_params,
    get_lr,
    LabelSmoothing,
)
from utils.schedulers import get_policy
from utils.conv_type import GetGlobalSubnet


from args import args
import importlib

import data
import models


def main():
    print(args)
    torch.autograd.set_detect_anomaly(True)
    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
# Set to make training deterministic for seed
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    args.distributed = False

    # Simply call main_worker function
    main_worker(args)


def main_worker(args):
    args.gpu = None
    train, validate, modifier = get_trainer(args)

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    # create model and optimizer
    model = get_model(args)
    model = set_gpu(args, model)
    #print(list(model.parameters()))
    if args.pretrained:
        pretrained(args, model)

        # Assuming args.pretrained points to your .pt file
        pretrained_path = "resnet18_fine_tuned_CIFAR10.pt"

        # Load the pre-trained model weights
        if os.path.isfile(pretrained_path):
            print(f"=> loading pretrained model from '{pretrained_path}'")
            pretrained_dict = torch.load(pretrained_path)

            # Assuming the .pt file contains only the state_dict
            if 'state_dict' in pretrained_dict:
                pretrained_dict = pretrained_dict['state_dict']

            model_dict = model.state_dict()

            # Filter out unnecessary keys
            pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}

            # Overwrite entries in the existing state dict
            model_dict.update(pretrained_dict)

            # Load the new state dict into the model
            model.load_state_dict(model_dict)

            print("=> loaded pretrained model")
        else:
            print(f"=> no pretrained model found at '{pretrained_path}'")

    optimizer = get_optimizer(args, model)
    data, train_augmentation = get_dataset(args)
    lr_policy = get_policy(args.lr_policy)(optimizer, args)

    if args.label_smoothing is None:
        criterion = nn.CrossEntropyLoss().cuda()
    else:
        criterion = LabelSmoothing(smoothing=args.label_smoothing)

    # optionally resume from a checkpoint
    acc1 = 0.0
    acc5 = 0.0
    best_acc1 = 0.0
    best_acc5 = 0.0
    best_train_acc1 = 0.0
    best_train_acc5 = 0.0

    if args.resume:
        best_acc1 = resume(args, model, optimizer)
        print("ARGS.RESUME")

    # Data loading code
    if args.evaluate:
        acc1, acc5 = validate(
            data.val_loader, model, criterion, args, writer=None, epoch=args.start_epoch
        )
        print("ARGS.EVALUATE")

        return

    # Set up directories
    run_base_dir, ckpt_base_dir, log_base_dir = get_directories(args)
    args.ckpt_base_dir = ckpt_base_dir

    print("RUN DIR: ", run_base_dir)

    writer = SummaryWriter(log_dir=log_base_dir)
    epoch_time = AverageMeter("epoch_time", ":.4f", write_avg=False)
    validation_time = AverageMeter("validation_time", ":.4f", write_avg=False)
    train_time = AverageMeter("train_time", ":.4f", write_avg=False)
    progress_overall = ProgressMeter(
        1, [epoch_time, validation_time, train_time], prefix="Overall Timing"
    )

    end_epoch = time.time()
    args.start_epoch = args.start_epoch or 0
    acc1 = None

    # Save the initial state
    save_checkpoint(
        {
            "epoch": 0,
            "arch": args.arch,
            "state_dict": model.state_dict(),
            "best_acc1": best_acc1,
            "best_acc5": best_acc5,
            "best_train_acc1": best_train_acc1,
            "best_train_acc5": best_train_acc5,
            "optimizer": optimizer.state_dict(),
            "curr_acc1": acc1 if acc1 else "Not evaluated",
            "conv_type": args.conv_type,
            "prune_rate": args.prune_rate,
            "train_augmentation": train_augmentation,
            "use_augmix": args.augmix,
            "jsd": args.jsd,
            "augmix_mixture_width": args.mixture_width,
            "augmix_mixture_depth": args.mixture_depth,
            "augmix_severity": args.aug_severity,
            "use_gaussian_aug": args.gaussian_aug,
            "p_clean": args.p_clean,
            "std_gauss": args.std_gauss,
        },
        False,
        filename=ckpt_base_dir / f"initial.state",
        save=False,
    )

    # Set final_prune_rate for gradually increasing pruning rate in global pruning
    final_prune_rate = args.prune_rate

    # Start training
    # torch.cuda.empty_cache()
    for epoch in range(args.start_epoch, args.epochs):

        # If using global pruning, gradually increase pruning rate to avoid layer collapse
        if args.conv_type == "GlobalSubnetConv" and epoch < args.prune_rate_epoch:
          if args.prune_rate <= 0.5:
             prune_decay = (1 - (epoch/args.prune_rate_epoch))**3
             curr_prune_rate = (1-final_prune_rate) + ((0.5 - (1-final_prune_rate))*prune_decay)
             args.prune_rate = (1-curr_prune_rate)
             print("args.prune_rate = ", args.prune_rate)
        elif args.conv_type == "GlobalSubnetConv" and epoch == args.prune_rate_epoch:
          args.prune_rate = final_prune_rate
          print("args.prune_rate = ", args.prune_rate)


        lr_policy(epoch, iteration=None)
        modifier(args, epoch, model)

        cur_lr = get_lr(optimizer)
        # torch.nn.utils.clip_grad_value_(model.parameters(),2)

        # train for one epoch
        start_train = time.time()
        train_acc1, train_acc5 = train(
            data.train_loader, model, criterion, optimizer, epoch, args, writer=writer
        )
        train_time.update((time.time() - start_train) / 60)

        # evaluate on validation set
        start_validation = time.time()
        acc1, acc5 = validate(data.val_loader, model, criterion, args, writer, epoch)
        validation_time.update((time.time() - start_validation) / 60)

        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)
        best_acc5 = max(acc5, best_acc5)
        best_train_acc1 = max(train_acc1, best_train_acc1)
        best_train_acc5 = max(train_acc5, best_train_acc5)

        if best_acc1 < 2 and epoch > 1:
           raise SystemExit("Terminating early: Network is not learning") 

        save = ((epoch % args.save_every) == 0) and args.save_every > 0
        if is_best or save or epoch == args.epochs - 1:
            if is_best:
                print(f"==> New best {best_acc1}, saving at {ckpt_base_dir / 'model_best.pth'}")

            save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "arch": args.arch,
                    "state_dict": model.state_dict(),
                    "best_acc1": best_acc1,
                    "best_acc5": best_acc5,
                    "best_train_acc1": best_train_acc1,
                    "best_train_acc5": best_train_acc5,
                    "optimizer": optimizer.state_dict(),
                    "curr_acc1": acc1,
                    "curr_acc5": acc5,
                    "conv_type": args.conv_type,
                    "prune_rate": args.prune_rate,
                    "train_augmentation": train_augmentation,
                    "use_augmix": args.augmix,
                    "jsd": args.jsd,
                    "augmix_mixture_width": args.mixture_width,
                    "augmix_mixture_depth": args.mixture_depth,
                    "augmix_severity": args.aug_severity,
                    "use_gaussian_aug": args.gaussian_aug,
                    "p_clean": args.p_clean,
                    "std_gauss": args.std_gauss,
                },
                is_best,
                filename=ckpt_base_dir / f"epoch_most_recent.state",
                save=save,
            )

            save_checkpoint(
                {
                    "epoch": epoch + 1,
                    "arch": args.arch,
                    "state_dict": model.state_dict(),
                    "best_acc1": best_acc1,
                    "best_acc5": best_acc5,
                    "best_train_acc1": best_train_acc1,
                    "best_train_acc5": best_train_acc5,
                    "optimizer": optimizer.state_dict(),
                    "curr_acc1": acc1,
                    "curr_acc5": acc5,
                    "conv_type": args.conv_type,
                    "prune_rate": args.prune_rate,
                    "train_augmentation": train_augmentation,
                    "use_augmix": args.augmix,
                    "jsd": args.jsd,
                    "augmix_mixture_width": args.mixture_width,
                    "augmix_mixture_depth": args.mixture_depth,
                    "augmix_severity": args.aug_severity,
                    "use_gaussian_aug": args.gaussian_aug,
                    "p_clean": args.p_clean,
                    "std_gauss": args.std_gauss,
                },
                is_best,
                filename=ckpt_base_dir / f"epoch_{epoch}.state",
                save=save,
            )
            #filename=ckpt_base_dir / f"epoch_{epoch}.state",

        epoch_time.update((time.time() - end_epoch) / 60)
        progress_overall.display(epoch)
        progress_overall.write_to_tensorboard(
            writer, prefix="diagnostics", global_step=epoch
        )

        if args.conv_type == "SampleSubnetConv":
            count = 0
            sum_pr = 0.0
            for n, m in model.named_modules():
                if isinstance(m, SampleSubnetConv):
                    # avg pr across 10 samples
                    pr = 0.0
                    for _ in range(10):
                        pr += (
                            (torch.rand_like(m.clamped_scores) >= m.clamped_scores)
                            .float()
                            .mean()
                            .item()
                        )
                    pr /= 10.0
                    writer.add_scalar("pr/{}".format(n), pr, epoch)
                    sum_pr += pr
                    count += 1

            args.prune_rate = sum_pr / count
            writer.add_scalar("pr/average", args.prune_rate, epoch)

        writer.add_scalar("test/lr", cur_lr, epoch)
        end_epoch = time.time()
        #print("EPOCH TIME: ", end_epoch-start_train)
        #torch.cuda.empty_cache()

    # Finalize prune rate for globally pruned networks
    if args.conv_type == "GlobalSubnetConv":
      global_pr, prune_dict = global_prune_rate(model, args)
      # Save prune rate dictionary to file in checkpoint directory
      dict_filename=f"{ckpt_base_dir}/global_prune_rate_dictionary.pkl"
      with open(dict_filename, 'wb') as f:
        pickle.dump(prune_dict, f)
    else:
      # For layerwise pruning, global prune rate is layer prune rate
      global_pr = args.prune_rate

    #print("BEST ACC 1: ", best_acc1)
    write_result_to_csv(
        best_acc1=best_acc1,
        best_acc5=best_acc5,
        best_train_acc1=best_train_acc1,
        best_train_acc5=best_train_acc5,
        prune_rate=args.prune_rate,
        curr_acc1=acc1,
        curr_acc5=acc5,
        base_config=args.config,
        seed=args.seed,
        name=args.name,
        lr=args.lr,
        weight_decay=args.weight_decay,
        epochs=args.epochs,
        learn_bn=args.learn_batchnorm,
        tune_bn=args.tune_batchnorm,
        bias_only=args.bn_bias_only,
        run_base_dir=run_base_dir,
    )


def get_trainer(args):
    print(f"=> Using trainer from trainers.{args.trainer}")
    trainer = importlib.import_module(f"trainers.{args.trainer}")

    return trainer.train, trainer.validate, trainer.modifier


def set_gpu(args, model):
    assert torch.cuda.is_available(), "CPU-only experiments currently unsupported"

    if args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        model = model.cuda(args.gpu)
    elif args.multigpu is None:
        device = torch.device("cpu")
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        print(f"=> Parallelizing on {args.multigpu} gpus")
        torch.cuda.set_device(args.multigpu[0])
        args.gpu = args.multigpu[0]
        model = torch.nn.DataParallel(model, device_ids=args.multigpu).cuda(
            args.multigpu[0]
        )
    if args.seed is None:
        cudnn.benchmark = True

    return model


def resume(args, model, optimizer):
    if os.path.isfile(args.resume):
        print(f"=> Loading checkpoint '{args.resume}'")

        checkpoint = torch.load(args.resume, map_location=f"cuda:{args.multigpu[0]}")
        if args.start_epoch is None:
            print(f"=> Setting new start epoch at {checkpoint['epoch']}")
            args.start_epoch = checkpoint["epoch"]

        best_acc1 = checkpoint["best_acc1"]

        model.load_state_dict(checkpoint["state_dict"])

        optimizer.load_state_dict(checkpoint["optimizer"])

        print(f"=> Loaded checkpoint '{args.resume}' (epoch {checkpoint['epoch']})")

        return best_acc1
    else:
        print(f"=> No checkpoint found at '{args.resume}'")


def pretrained(args, model):
    if os.path.isfile(args.pretrained):
        print("=> loading pretrained weights from '{}'".format(args.pretrained))
        pretrained = torch.load(
            args.pretrained,
            map_location=torch.device("cuda:{}".format(args.multigpu[0])),
        )["state_dict"]

        model_state_dict = model.state_dict()
        for k, v in pretrained.items():
            if k not in model_state_dict or v.size() != model_state_dict[k].size():
                print("IGNORE:", k)
        pretrained = {
            k: v
            for k, v in pretrained.items()
            if (k in model_state_dict and v.size() == model_state_dict[k].size())
        }
        model_state_dict.update(pretrained)
        model.load_state_dict(model_state_dict)

    else:
        print("=> no pretrained weights found at '{}'".format(args.pretrained))

    for n, m in model.named_modules():
        if isinstance(m, FixedSubnetConv):
            m.set_subnet()


def get_dataset(args):
    train_augmentation = 'Default'
    # Check if gaussian augmenation is being used
    if args.gaussian_aug:
      # Add _gaussian to args.set
      args.set = args.set + '_gaussian'
      # Set train augmentation to gaussian for logging purposes
      train_augmentation = 'Gaussian'
    # Check if augmix is being used
    elif args.augmix:
      # Add _augmix to args.set
      args.set = args.set + '_augmix'
      # Set train augmentation to augmix for logging purposes
      train_augmentation = 'Augmix'
    print(f"=> Getting {args.set} dataset")
    dataset = getattr(data, args.set)(args)

    return dataset, train_augmentation


def get_model(args):
    if args.first_layer_dense:
        args.first_layer_type = "DenseConv"

    print("=> Creating model '{}'".format(args.arch))
    model = models.__dict__[args.arch]()

    # applying sparsity to the network
    if (
        args.conv_type != "DenseConv"
        and args.conv_type != "SampleSubnetConv"
        and args.conv_type != "ContinuousSparseConv"
    ):
        if args.prune_rate < 0:
            raise ValueError("Need to set a positive prune rate")

        set_model_prune_rate(model, prune_rate=args.prune_rate)
        print(
            f"=> Rough estimate model params {sum(int(p.numel() * (1-args.prune_rate)) for n, p in model.named_parameters() if not n.endswith('scores'))}"
        )
    if args.bn_weight_init is not None or args.bn_bias_init is not None:
        bn_weight_init(model, weight=args.bn_weight_init, bias=args.bn_bias_init)

    # freezing the weights if we are only doing subnet training
    if args.freeze_weights:
        freeze_model_weights(model)

    return model


def get_optimizer(args, model):
    for n, v in model.named_parameters():
        if v.requires_grad:
            print("<DEBUG> gradient to", n)

        if not v.requires_grad:
            print("<DEBUG> no gradient to", n)
    print("OPTIMIZER: ", args.optimizer)
    if args.optimizer == "sgd":
        parameters = list(model.named_parameters())
        bn_params = [v for n, v in parameters if ("bn" in n) and v.requires_grad]
        rest_params = [v for n, v in parameters if ("bn" not in n) and v.requires_grad]
        optimizer = torch.optim.SGD(
            [
                {
                    "params": bn_params,
                    "weight_decay": 0 if args.no_bn_decay else args.weight_decay,
                },
                {"params": rest_params, "weight_decay": args.weight_decay},
            ],
            args.lr,
            momentum=args.momentum,
            weight_decay=args.weight_decay,
            nesterov=args.nesterov,
        )
    elif args.optimizer == "adam":
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr
        )
    #print(list(model.parameters()))

    return optimizer


def _run_dir_exists(run_base_dir):
    log_base_dir = run_base_dir / "logs"
    ckpt_base_dir = run_base_dir / "checkpoints"

    return log_base_dir.exists() or ckpt_base_dir.exists()


def get_directories(args):
    if args.config is None or args.name is None:
        raise ValueError("Must have name and config")

    config = pathlib.Path(args.config).stem
    if args.log_dir is None:
        run_base_dir = pathlib.Path(
            f"runs/{config}/{args.name}/prune_rate={args.prune_rate}"
        )
    else:
        run_base_dir = pathlib.Path(
            f"{args.log_dir}/{config}/{args.name}/prune_rate={args.prune_rate}"
        )
    if args.width_mult != 1.0:
        run_base_dir = run_base_dir / "width_mult={}".format(str(args.width_mult))

    if _run_dir_exists(run_base_dir):
        rep_count = 0
        while _run_dir_exists(run_base_dir / str(rep_count)):
            rep_count += 1

        run_base_dir = run_base_dir / str(rep_count)

    log_base_dir = run_base_dir / "logs"
    ckpt_base_dir = run_base_dir / "checkpoints"

    if not run_base_dir.exists():
        os.makedirs(run_base_dir)

    (run_base_dir / "settings.txt").write_text(str(args))

    return run_base_dir, ckpt_base_dir, log_base_dir


def write_result_to_csv(**kwargs):
    results = pathlib.Path("runs") / "indiv_results4.csv"
    if args.results is not None:
        results = pathlib.Path(args.results) 

    if not results.exists():
        results.write_text(
            "Date Finished, "
            "Base Config, "
            "Current Val Top 1, "
            "Current Val Top 5, "
            "Best Val Top 1, "
            "Best Val Top 5, "
            "Best Train Top 1, "
            "Best Train Top 5, "
            "Name, "
            "Seed, "
            "Prune Rate, "
            "Learning Rate, "
            "Epochs, "
            "Weight Decay, "
            "Learn BN, "
            "Tune BN, "
            "Bias Only, "
            "Run Directory\n"
        )

    now = time.strftime("%m-%d-%y_%H:%M:%S")

    with open(results, "a+") as f:
        f.write(
            (
                "{now}, "
                "{base_config}, "
                "{curr_acc1:.02f}, "
                "{curr_acc5:.02f}, "
                "{best_acc1:.02f}, "
                "{best_acc5:.02f}, "
                "{best_train_acc1:.02f}, "
                "{best_train_acc5:.02f}, "
                "{name}, "
                "{seed}, "
                "{prune_rate}, "
                "{lr}, "
                "{epochs}, "
                "{weight_decay}, "
                "{learn_bn}, "
                "{tune_bn}, "
                "{bias_only}, "
                "{run_base_dir}\n"
            ).format(now=now, **kwargs)
        )

# Compute global prune rate at end of training
def global_prune_rate(model, args):
    # Initialize dictionary to store prune rates for each layer
    prune_dict = {}
    # Print breakdown of prune rate by layer
    print("\n==> Final layerwise prune rates in network:")
    # Loop over all model parameters and compute percentage of weights pruned globally
    total_weights = 0
    unpruned_weights = 0
    # Loop over all model parameters to get sparsity of each layer
    for n, m in model.named_modules():
      # Only add parameters that have prune_threshold as attribute
      if hasattr(m,'prune_threshold'):
        tmp_scores = m.clamped_scores.clone().detach()
        # Add to total_weights
        layer_total = int(torch.numel(tmp_scores))
        #print("Total number of weights in layer = ", t)
        total_weights += layer_total
        # Compute layer prune rate (doesn't seem to be stored correctly during multigpu runs)
        w = GetGlobalSubnet.apply(tmp_scores, m.weight.detach().clone(), m.prune_threshold)
        # Compute number of unpruned weights in layer
        layer_unpruned = torch.count_nonzero(w).item()
        # Compute pruning rate for current layer
        layer_prune_rate = 1 - (layer_unpruned/layer_total)
        # Compute number of pruned weights
        print("%s prune percentage: %lg" %(n,100*layer_prune_rate))
        unpruned_weights += layer_unpruned
        # Add prune_rate for current layer to dictionary
        prune_dict[n] = 100*layer_prune_rate
        # Set prune threshold value (same for all layers)
        pr_thresh = m.prune_threshold

    # Compute global pruning percentage
    #print ("total_weights = ", total_weights)
    #print ("pruned_weights = ", unpruned_weights)
    final_prune_rate = (1 - (unpruned_weights/total_weights))
    #print("Global pruning percentage: ", 100 * final_prune_rate)
    print("\n==> Global prune rate: ", 1-final_prune_rate)

    #print("\n==> Final prune threshold value: ", pr_thresh)

    # Return global prune rate
    return (1-final_prune_rate), prune_dict



if __name__ == "__main__":
    main()
