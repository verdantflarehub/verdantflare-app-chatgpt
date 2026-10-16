# VerdantFlare ChatGPT Channel

## 1. 实现状态

独立 ChatGPT 订阅网关账号管理与 AppDashboard。已实现加密账号、组织隔离、明确默认账号、启停归档、连接原子轮换、会员登记和网关只读观测。会员等级、会员到期、5 小时与 7 天用量分别保存；网关额度保留实际单位，不当作会员现金余额。

产品和接口事实源位于设计仓库 `docs/design/app/chatgpt/README.md` 与 `docs/design/contracts/channel-provider-v1.md`。本项目尚未发布；图片执行、旧连接迁移、Network 代理、价格结算及 etcd 仍未接通。健康接口不声明已支持生成，页面不能作为在线验收证据。

## 2. 服务入口与配置

入口为 `python -m chatgpt_channel.server`，管理端口 8000 仅供 Studio 内部访问。数据库由 `CHATGPT_STATE_DB` 指定，服务持有状态卷执行锁；凭据从 `CHATGPT_MANAGEMENT_TOKEN`、`CHATGPT_ENCRYPTION_KEY` 读取，不回显到页面。

`CHATGPT_ALLOWED_API_ORIGINS` 是管理员配置的 HTTPS 网关 origin 白名单，逗号分隔，不含路径。无默认 OpenAI Platform 地址；连接基地址规范化为 `/v1`，只读更新调用 `/v1/usage`。客户端禁用环境代理继承与重定向。目前仅接受明确直连，代理引用尚待 Network 接线。

`CHATGPT_DASHBOARD_DIR` 指向校验摘要的页面包。源码页面通过 `python -m chatgpt_channel.dashboard --source dashboard --output <空输出目录>` 生成；正式服务启动、浏览器验收在开发集群进行，本机不启动服务。

## 3. 轻量验证

安装项目声明依赖后，使用 `python -m unittest discover -s tests`、`node --test tests/dashboard.test.mjs`、`node --check dashboard/dashboard.mjs` 和 `python -m compileall -q chatgpt_channel`。测试使用合成账号、临时 SQLite 和模拟网关响应，不触发生成。

## 4. 构建交付

Actions 工作流 `.github/workflows/chatgpt-channel.yml` 负责 release 分支的生产镜像构建与推送，版本 0.1.0；镜像采用非 root 用户，仅暴露已实现的管理监听。工作流从发布镜像摘要提取页面资源及发行索引，不把本地模拟制品当成已发布版本。远端仓库、Registry Secrets、首次 Actions 运行与集群注册尚未完成。

正式发布后，使用设计仓库 `scripts/appdashboard/prepare_channel.py --app-id chatgpt`，结合独立核验的源码提交及镜像摘要生成管理登记片段。该工具仅准备材料，不执行部署或安装。
