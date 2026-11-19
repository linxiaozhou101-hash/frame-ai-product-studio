"""Submit one real protected background-edit task and save an audit record."""

import argparse
import json
from pathlib import Path

from bailian import BailianClient
from config import Settings
from images import read_product_image
from prompts import STYLES
from protection import prepare_product
from workflow import Job, run_job


def main() -> int:
    parser = argparse.ArgumentParser(description="调用真实百炼局部重绘 API，提交一张图片（按服务规则计费）。")
    parser.add_argument("image", type=Path, help="≤10 MB 的 JPG/PNG")
    parser.add_argument("--style", choices=list(STYLES), default="简洁摄影棚")
    parser.add_argument("--mask", type=Path, help="可选商品蒙版，白色商品、黑色背景")
    parser.add_argument("--output", type=Path, default=Path("result.png"))
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        product = read_product_image(args.image.read_bytes())
        print("正在识别商品轮廓（首次运行需下载本地分割模型）…", flush=True)
        prepared = prepare_product(product, mask_data=args.mask.read_bytes() if args.mask else None)
    except (ValueError, OSError) as exc:
        print(f"无法开始：{exc}")
        return 1
    job = Job(product, args.style, model=settings.model, prepared=prepared)
    with BailianClient(settings) as client:
        run_job(job, client, poll_interval=settings.poll_interval_seconds,
                timeout=settings.task_timeout_seconds,
                on_update=lambda phase, elapsed: print(f"[{elapsed:5.1f}s] {phase}", flush=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    record = {"task_id": job.task_id, "model": job.model, "style": job.style,
              "status": job.task_status, "elapsed_seconds": round(job.elapsed, 2),
              "error_code": job.error_code, "prompt": job.submitted_prompt}
    args.output.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if job.error:
        print(f"未完成：{job.error}（{job.error_code}）\n任务 ID：{job.task_id or '未获取'}")
        print("已有任务请根据任务 ID 在控制台查询；重新执行脚本会提交新任务。")
        return 1
    args.output.write_bytes(job.result)
    print(f"已保存：{args.output.resolve()}\n任务 ID：{job.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
