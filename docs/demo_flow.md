# 本地演示流程

按 README 安装并启动，模型设置页先确认连接。无密钥演示用 `.env` 中 `MODEL_PROVIDER=mock`、`SEARCH_PROVIDER=mock`，保持 `DEPLOY_MODE=local` 与 `s_team / t1 / g2`。不要切换设置页的搜索开关；它会启用真实搜索。

## 中文：朋友聚会

1. 打开 `/zh/`，点「开始」进入问卷。
2. 填 4 人 → 没人当主持 → 只用通用用品 → 朋友聚会 → 轻松聊天 → 补充偏好填「第一次玩，想先熟悉流程」。展示五项原有问卷和新增的选填项。
3. 点「查看推荐」，看到三张卡片及 Mock 来源。可先展示补充偏好或换一批；正式演示带玩时保持首次结果，避免改变选定游戏。
4. 打开第一张卡片，展示立即可读的目录规则与 AI 新手讲解。
5. 点「开始带玩」→「跳过名字」，等待计划。选中的游戏若没有本地规则书且没有缓存，可展示搜索、取网页工具卡与网络来源；mock 搜索使用固定示例内容，需明确告诉观众它是流程模拟。
6. 点「下一步」，展示步骤变化；点骰子和记分工具，展示状态更新；输入「帮我掷一次骰子」，展示对话中的工具事件。

## English: friends learning together

1. Open `/en/` and start the questionnaire.
2. Enter 4 players → No dedicated host → Basic supplies only → Friends' get-together → Relaxed conversation → “First time playing. Please guide us step by step.”
3. Show the recommendations, open the first card, and read the catalog rules and AI explanation.
4. Choose “Start guided play”, skip player names, and wait for the plan. Show source badges and one “Next step” action.
5. Ask “Please roll a die”, then show the dice tool event and the updated state. Refresh once to show session recovery.

中英文场景使用相同硬条件，便于检查页面语言切换和同一产品流程。mock 的回复来自固定 fixtures，部分文案语言与当前页面可能不同；它不证明模型的双语生成质量。

## 备用路径

- 真实模型不可用：停止服务，将 `MODEL_PROVIDER=mock`、`SEARCH_PROVIDER=mock` 写入 `.env` 后重启，回到首页重新填问卷。切换 mock 不需要真实密钥，也不进行真实连接测试。
- 已有同条件 plan：重新创建会话可命中缓存；缓存命中不重新搜索，因此需要展示搜索事件时用全新的本地运行数据库，而不是删除已有用户数据。
- 讲解请求失败：详情仍可读目录规则；带玩尚未生成有效计划时，先展示详情并重试，不能将失败说成已生成。
- 本次只演示本机使用，无账号登录或多人部署步骤。
