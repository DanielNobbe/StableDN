#!/bin/bash
CKPT_PATH="StableSR/stablesr_000117.ckpt"
VQGANCKPT_PATH="StableSR/vqgan_cfw_00011.ckpt"
INPUT_PATH="inputs/mine"
OUT_DIR="output"

# python -c "import os; print(os.getcwd()); from ldm.util import instantiate_from_config"
python scripts/sr_val_ddpm_text_T_vqganfin_old.py --config configs/stableSRNew/v2-finetune_text_T_512.yaml --ckpt $CKPT_PATH --vqgan_ckpt $VQGANCKPT_PATH --init-img $INPUT_PATH --outdir $OUT_DIR --ddpm_steps 200 --dec_w 0.5 --colorfix_type adain