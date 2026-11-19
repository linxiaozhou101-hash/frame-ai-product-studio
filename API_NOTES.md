# 百炼接入决策

核对日期：2026-11-19。此项目使用真实异步图像编辑接口。

## 当前协议

`wanx2.1-imageedit` 使用北京地域。`POST /api/v1/services/aigc/image2image/image-synthesis` 提交；请求头包含 `Authorization: Bearer <环境变量中的 Key>`、`Content-Type: application/json`、`X-DashScope-Async: enable`。

请求体结构：

```json
{
  "model": "wanx2.1-imageedit",
  "input": {
    "function": "description_edit_with_mask",
    "prompt": "描述具体的新背景，并保留商品外观与拍摄角度",
    "base_image_url": "data:image/png;base64,<商品图片>",
    "mask_image_url": "data:image/png;base64,<背景编辑蒙版>"
  },
  "parameters": {"n": 1, "watermark": false}
}
```

图片和编辑蒙版必须同尺寸，宽高各 512–4096 像素，各不超过 10 MB。**API 蒙版中纯白编辑、纯黑保留**；上传给页面的“商品蒙版”语义相反，需要转换，不能直接发送。API 蒙版使用二值图，合成边缘另用 alpha。

保存 `output.task_id` 后，`GET /api/v1/tasks/{task_id}` 查询。检查 `output.task_status`；成功时读取 `output.results[].url`，失败时读取错误码和信息。查询或下载失败应优先恢复原任务，避免重复提交；任务与结果链接有效期为 24 小时。

[官方 2.1 API 文档](https://help.aliyun.com/zh/model-studio/wanx-image-edit-api-reference)

## 效果改进的依据

原实现只靠全图编辑指令保护商品，无法约束文字、图案等细节。现改用背景蒙版限定编辑区域，并把原商品像素合成回生成背景。这个策略的效果仍取决于商品轮廓质量，透明材质、反光与复杂边缘需要人工检查。

背景大面积替换时，提示词优先描述最终场景，包括背景材质、台面、照明和接触阴影，再加入商品保持要求；单独写“删除背景”信息不足。[官方 2.1 使用指南](https://help.aliyun.com/zh/model-studio/wanx-image-edit)

`strength` 的官方适用范围为全局风格化和普通指令编辑，因此局部重绘不发送该参数。

## 为什么不直接切换 2.7

2.7 的 `bbox_list` 每张图最多接受两个矩形框，没有同等的逐像素背景蒙版参数；矩形框不能准确表达杯把孔洞或不规则商品边缘。它的请求结构、端点也不同，不能仅改模型名就沿用 2.1 请求。此项目优先实现可检查的区域约束，未把模型升级当作已验证的效果修复。

[官方 2.7 API 文档](https://help.aliyun.com/zh/model-studio/wan-image-generation-and-editing-api-reference)
