# COT 指标看板

- `pine/`：TradingView Pine v6 指标（净持仓、COT 指数）
- `scripts/fetch_cot.py`：下载 CFTC Disaggregated 报告（期货+期权合并，仅黄金/白银），生成 `docs/data/cot.json`，仅用标准库
- `docs/index.html`：静态网页，GitHub Pages 发布源（Settings → Pages → Branch: main, Folder: /docs）
- `.github/workflows/update.yml`：每周五/六自动更新数据

本地预览：先 `python scripts/fetch_cot.py`，再在 `docs/` 下运行 `python -m http.server`。
