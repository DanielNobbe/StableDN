"""make variations of input image"""

import argparse, os, sys, glob
import PIL
import torch
import numpy as np
import torchvision
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange, repeat
from torchvision.utils import make_grid
from torch import autocast
from contextlib import nullcontext
import time
from pytorch_lightning import seed_everything

from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from basicsr.metrics import calculate_niqe
import math
import copy
import torch.nn.functional as F
import cv2
from util_image import ImageSpliterTh
from pathlib import Path

def exact_feature_distribution_matching(content, style):
	assert (content.size() == style.size()) ## content and style features should share the same shape
	B, C, W, H = content.size(0), content.size(1), content.size(2), content.size(3)
	_, index_content = torch.sort(content.reshape(B,C,-1))  ## sort content feature
	value_style, _ = torch.sort(style.reshape(B,C,-1))      ## sort style feature
	inverse_index = index_content.argsort(-1)
	transferred_content = content.reshape(B,C,-1) + value_style.gather(-1, inverse_index) - content.reshape(B,C,-1).detach()
	return transferred_content.reshape(B, C, W, H)

def get_mean_and_std(x):
	x_mean, x_std = cv2.meanStdDev(x)
	x_mean = np.hstack(np.around(x_mean,2))
	x_std = np.hstack(np.around(x_std,2))
	return x_mean, x_std

def calc_mean_std(feat, eps=1e-5):
	"""Calculate mean and std for adaptive_instance_normalization.
	Args:
		feat (Tensor): 4D tensor.
		eps (float): A small value added to the variance to avoid
			divide-by-zero. Default: 1e-5.
	"""
	size = feat.size()
	assert len(size) == 4, 'The input feature should be 4D tensor.'
	b, c = size[:2]
	feat_var = feat.reshape(b, c, -1).var(dim=2) + eps
	feat_std = feat_var.sqrt().reshape(b, c, 1, 1)
	feat_mean = feat.reshape(b, c, -1).mean(dim=2).reshape(b, c, 1, 1)
	return feat_mean, feat_std

def adaptive_instance_normalization(content_feat, style_feat):
	"""Adaptive instance normalization.
	Adjust the reference features to have the similar color and illuminations
	as those in the degradate features.
	Args:
		content_feat (Tensor): The reference feature.
		style_feat (Tensor): The degradate features.
	"""
	size = content_feat.size()
	style_mean, style_std = calc_mean_std(style_feat)
	content_mean, content_std = calc_mean_std(content_feat)
	normalized_feat = (content_feat - content_mean.expand(size)) / content_std.expand(size)
	return normalized_feat * style_std.expand(size) + style_mean.expand(size)

def space_timesteps(num_timesteps, section_counts):
	"""
	Create a list of timesteps to use from an original diffusion process,
	given the number of timesteps we want to take from equally-sized portions
	of the original process.
	For example, if there's 300 timesteps and the section counts are [10,15,20]
	then the first 100 timesteps are strided to be 10 timesteps, the second 100
	are strided to be 15 timesteps, and the final 100 are strided to be 20.
	If the stride is a string starting with "ddim", then the fixed striding
	from the DDIM paper is used, and only one section is allowed.
	:param num_timesteps: the number of diffusion steps in the original
						  process to divide up.
	:param section_counts: either a list of numbers, or a string containing
						   comma-separated numbers, indicating the step count
						   per section. As a special case, use "ddimN" where N
						   is a number of steps to use the striding from the
						   DDIM paper.
	:return: a set of diffusion steps from the original process to use.
	"""
	if isinstance(section_counts, str):
		if section_counts.startswith("ddim"):
			desired_count = int(section_counts[len("ddim"):])
			for i in range(1, num_timesteps):
				if len(range(0, num_timesteps, i)) == desired_count:
					return set(range(0, num_timesteps, i))
			raise ValueError(
				f"cannot create exactly {num_timesteps} steps with an integer stride"
			)
		section_counts = [int(x) for x in section_counts.split(",")]   #[250,]
	size_per = num_timesteps // len(section_counts)
	extra = num_timesteps % len(section_counts)
	start_idx = 0
	all_steps = []
	for i, section_count in enumerate(section_counts):
		size = size_per + (1 if i < extra else 0)
		if size < section_count:
			raise ValueError(
				f"cannot divide section of {size} steps into {section_count}"
			)
		if section_count <= 1:
			frac_stride = 1
		else:
			frac_stride = (size - 1) / (section_count - 1)
		cur_idx = 0.0
		taken_steps = []
		for _ in range(section_count):
			taken_steps.append(start_idx + round(cur_idx))
			cur_idx += frac_stride
		all_steps += taken_steps
		start_idx += size
	return set(all_steps)

def chunk(it, size):
	it = iter(it)
	return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(config, ckpt, verbose=False):
	print(f"Loading model from {ckpt}")
	pl_sd = torch.load(ckpt, map_location="cpu")
	if "global_step" in pl_sd:
		print(f"Global Step: {pl_sd['global_step']}")
	sd = pl_sd["state_dict"]
	model = instantiate_from_config(config.model)
	m, u = model.load_state_dict(sd, strict=False)
	if len(m) > 0 and verbose:
		print("missing keys:")
		print(m)
	if len(u) > 0 and verbose:
		print("unexpected keys:")
		print(u)

	model.cuda()
	model.eval()
	return model

def load_img(path):
	image = Image.open(path).convert("RGB")
	w, h = image.size
	print(f"loaded input image of size ({w}, {h}) from {path}")
	w, h = map(lambda x: x - x % 8, (w, h))  # resize to integer multiple of 32
	image = image.resize((w, h), resample=PIL.Image.LANCZOS)
	image = np.array(image).astype(np.float32) / 255.0
	image = image[None].transpose(0, 3, 1, 2)
	image = torch.from_numpy(image)
	return 2.*image - 1.

def read_image(im_path):
	im = np.array(Image.open(im_path).convert("RGB"))
	im = im.astype(np.float32)/255.0
	im = im[None].transpose(0,3,1,2)
	im = (torch.from_numpy(im) - 0.5) / 0.5

	return im.cuda()

def main():
	parser = argparse.ArgumentParser()

	parser.add_argument(
		"--init-img",
		type=str,
		nargs="?",
		help="path to the input image",
		default="/dataset/ImageSR/RealSRSet/"
	)

	parser.add_argument(
		"--outdir",
		type=str,
		nargs="?",
		help="dir to write results to",
		default="outputs/sr-samples"
	)

	parser.add_argument(
		"--skip_grid",
		action='store_true',
		help="do not save a grid, only individual samples. Helpful when evaluating lots of samples",
	)

	parser.add_argument(
		"--skip_save",
		action='store_true',
		help="do not save indiviual samples. For speed measurements.",
	)
	parser.add_argument(
		"--ddpm_steps",
		type=int,
		default=1000,
		help="number of ddpm sampling steps",
	)
	parser.add_argument(
		"--n_iter",
		type=int,
		default=1,
		help="sample this often",
	)
	parser.add_argument(
		"--C",
		type=int,
		default=4,
		help="latent channels",
	)
	parser.add_argument(
		"--f",
		type=int,
		default=8,
		help="downsampling factor, most often 8 or 16",
	)
	parser.add_argument(
		"--n_samples",
		type=int,
		default=1,
		help="how many samples to produce for each given prompt. A.k.a batch size",
	)
	parser.add_argument(
		"--n_rows",
		type=int,
		default=0,
		help="rows in the grid (default: n_samples)",
	)

	parser.add_argument(
		"--config",
		type=str,
		default="configs/stable-diffusion/v1-inference.yaml",
		help="path to config which constructs model",
	)
	parser.add_argument(
		"--ckpt",
		type=str,
		default="models/ldm/stable-diffusion-v1/model.ckpt",
		help="path to checkpoint of model",
	)
	parser.add_argument(
        "--vqgan_ckpt",
        type=str,
        default="models/ldm/stable-diffusion-v1/epoch=000011.ckpt",
        help="path to checkpoint of VQGAN model",
    )
	parser.add_argument(
		"--seed",
		type=int,
		default=42,
		help="the seed (for reproducible sampling)",
	)
	parser.add_argument(
		"--precision",
		type=str,
		help="evaluate at this precision",
		choices=["full", "autocast"],
		default="autocast"
	)
	parser.add_argument(
		"--input_size",
		type=int,
		default=512,
		help="input size",
	)

	parser.add_argument(
		"--dec_w",
		type=float,
		default=1.0,
		help="weight for combining VQGAN and Diffusion",
	)
	parser.add_argument(
		"--tile_overlap",
		type=int,
		default=32,
		help="tile overlap size",
	)

	parser.add_argument(
		"--upscale",
		type=float,
		default=4.0,
		help="upsample scale",
	)
	parser.add_argument(
		"--nocolor",
		action='store_true',
		help="if cancel color correction",
	)

	opt = parser.parse_args()
	seed_everything(opt.seed)

	config = OmegaConf.load(f"{opt.config}")
	model = load_model_from_config(config, f"{opt.ckpt}")
	device = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
	model = model.to(device)

	model.configs = config

	vqgan_config = OmegaConf.load("configs/autoencoder/autoencoder_kl_64x64x4_resi.yaml")
	vq_model = load_model_from_config(vqgan_config, opt.vqgan_ckpt)
	vq_model = vq_model.to(device)
	vq_model.decoder.fusion_w = opt.dec_w

	os.makedirs(opt.outdir, exist_ok=True)
	outpath = opt.outdir

	batch_size = opt.n_samples
	n_rows = opt.n_rows if opt.n_rows > 0 else batch_size

	sample_path = os.path.join(outpath, "samples")
	os.makedirs(sample_path, exist_ok=True)
	input_path = os.path.join(outpath, "inputs")
	os.makedirs(input_path, exist_ok=True)
	base_count = len(os.listdir(sample_path))
	base_i = len(os.listdir(input_path))
	grid_count = len(os.listdir(outpath)) - 1

	images_path_ori = sorted(glob.glob(os.path.join(opt.init_img, "*.png")))
	images_path_ori.extend(sorted(glob.glob(os.path.join(opt.init_img, "*.jpg"))))
	images_path = copy.deepcopy(images_path_ori)
	for item in images_path_ori:
		img_name = item.split('/')[-1]
		if os.path.exists(os.path.join(outpath, img_name)):
			images_path.remove(item)
	print(f"Found {len(images_path)} inputs.")

	model.register_schedule(given_betas=None, beta_schedule="linear", timesteps=1000,
						  linear_start=0.00085, linear_end=0.0120, cosine_s=8e-3)
	model.num_timesteps = 1000

	model_ori = copy.deepcopy(model)

	use_timesteps = set(space_timesteps(1000, [opt.ddpm_steps]))
	last_alpha_cumprod = 1.0
	new_betas = []
	timestep_map = []
	for i, alpha_cumprod in enumerate(model.alphas_cumprod):
		if i in use_timesteps:
			new_betas.append(1 - alpha_cumprod / last_alpha_cumprod)
			last_alpha_cumprod = alpha_cumprod
			timestep_map.append(i)
	new_betas = [beta.data.cpu().numpy() for beta in new_betas]
	model.register_schedule(given_betas=np.array(new_betas), timesteps=len(new_betas))
	model.num_timesteps = 1000
	model.ori_timesteps = list(use_timesteps)
	model.ori_timesteps.sort()
	model = model.to(device)
	model_ori = model_ori.to(device)

	precision_scope = autocast if opt.precision == "autocast" else nullcontext
	niqe_list = []
	with torch.no_grad():
		with model.ema_scope():
			tic = time.time()
			all_samples = list()
			for n in trange(len(images_path), desc="Sampling"):
				if (n + 1) % opt.n_samples == 1 or opt.n_samples == 1:
					cur_image = read_image(images_path[n])
					size_min = min(cur_image.size(-1), cur_image.size(-2))
					upsample_scale = max(512/size_min, opt.upscale)
					cur_image = F.interpolate(
								cur_image,
								size=(int(cur_image.size(-2)*upsample_scale),
									  int(cur_image.size(-1)*upsample_scale)),
								mode='bicubic',
								)
					cur_image = cur_image.clamp(-1, 1)
					im_lq_bs = [cur_image, ]  # 1 x c x h x w, [-1, 1]
					im_path_bs = [images_path[n], ]
				else:
					cur_image = read_image(images_path[n])
					size_min = min(cur_image.size(-1), cur_image.size(-2))
					upsample_scale = max(512/size_min, opt.upscale)
					cur_image = F.interpolate(
								cur_image,
								size=(int(cur_image.size(-2)*upsample_scale),
									  int(cur_image.size(-1)*upsample_scale)),
								mode='bicubic',
								)
					cur_image = cur_image.clamp(-1, 1)
					im_lq_bs.append(cur_image) # 1 x c x h x w, [-1, 1]
					im_path_bs.append(images_path[n]) # 1 x c x h x w, [-1, 1]

				if (n + 1) % opt.n_samples == 0 or (n+1) == len(images_path):
					im_lq_bs = torch.cat(im_lq_bs, dim=0)
					ori_h, ori_w = im_lq_bs.shape[2:]
					ref_patch=None
					if not (ori_h % 32 == 0 and ori_w % 32 == 0):
						flag_pad = True
						pad_h = ((ori_h // 32) + 1) * 32 - ori_h
						pad_w = ((ori_w // 32) + 1) * 32 - ori_w
						im_lq_bs = F.pad(im_lq_bs, pad=(0, pad_w, 0, pad_h), mode='reflect')
					else:
						flag_pad = False

					ori_img = im_lq_bs.clone()

					if im_lq_bs.shape[2] > 1280 or im_lq_bs.shape[3] > 1280:
						im_spliter = ImageSpliterTh(im_lq_bs, 1280, 1000, sf=1)
						for im_lq_pch, index_infos in im_spliter:
							seed_everything(opt.seed)
							init_latent = model.get_first_stage_encoding(model.encode_first_stage(im_lq_pch))  # move to latent space
							text_init = ['']*opt.n_samples
							semantic_c = model.cond_stage_model(text_init)
							noise = torch.randn_like(init_latent)
							t = repeat(torch.tensor([999]), '1 -> b', b=im_lq_bs.size(0))
							t = t.to(device).long()
							x_T = model_ori.q_sample(x_start=init_latent, t=t, noise=noise)
							samples, _ = model.sample_canvas(cond=semantic_c, struct_cond=init_latent, batch_size=im_lq_pch.size(0), timesteps=opt.ddpm_steps, time_replace=opt.ddpm_steps, x_T=x_T, return_intermediates=True, tile_size=64, tile_overlap=opt.tile_overlap, batch_size_sample=opt.n_samples)
							_, enc_fea_lq = vq_model.encode(im_lq_pch)
							x_samples = vq_model.decode(samples * 1. / model.scale_factor, enc_fea_lq)
							if not opt.nocolor:
								x_samples = exact_feature_distribution_matching(x_samples, im_lq_pch)
							im_spliter.update(x_samples, index_infos)
						im_sr = im_spliter.gather()
						im_sr = torch.clamp((im_sr+1.0)/2.0, min=0.0, max=1.0)
					else:
						init_latent = model.get_first_stage_encoding(model.encode_first_stage(im_lq_bs))  # move to latent space
						text_init = ['']*opt.n_samples
						semantic_c = model.cond_stage_model(text_init)
						noise = torch.randn_like(init_latent)
						t = repeat(torch.tensor([999]), '1 -> b', b=im_lq_bs.size(0))
						t = t.to(device).long()
						x_T = model_ori.q_sample(x_start=init_latent, t=t, noise=noise)
						samples, _ = model.sample_canvas(cond=semantic_c, struct_cond=init_latent, batch_size=im_lq_bs.size(0), timesteps=opt.ddpm_steps, time_replace=opt.ddpm_steps, x_T=x_T, return_intermediates=True, tile_size=64, tile_overlap=opt.tile_overlap, batch_size_sample=opt.n_samples)
						_, enc_fea_lq = vq_model.encode(im_lq_bs)
						x_samples = vq_model.decode(samples * 1. / model.scale_factor, enc_fea_lq)
						if not opt.nocolor:
							x_samples = exact_feature_distribution_matching(x_samples, ori_img)
						im_sr = torch.clamp((x_samples+1.0)/2.0, min=0.0, max=1.0)

					if upsample_scale > opt.upscale:
						im_sr = F.interpolate(
									im_sr,
									size=(int(ori_img.size(-2)*opt.upscale/upsample_scale),
										  int(ori_img.size(-1)*opt.upscale/upsample_scale)),
									mode='bicubic',
									)
						im_sr = torch.clamp(im_sr, min=0.0, max=1.0)

					im_sr = im_sr.cpu().numpy().transpose(0,2,3,1)*255   # b x h x w x c

					if flag_pad:
						im_sr = im_sr[:, :ori_h*sf, :ori_w*sf, ]

					for jj in range(im_lq_bs.shape[0]):
						outpath = str(Path(opt.outdir) / Path(im_path_bs[jj]).name)
						Image.fromarray(im_sr[jj, ].astype(np.uint8)).save(outpath)

			toc = time.time()

	print(f"Your samples are ready and waiting for you here: \n{outpath} \n"
		  f" \nEnjoy.")


if __name__ == "__main__":
	main()