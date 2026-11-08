# Python任务工作流

从技能根运行scripts/project.py。下文python指已安装Pillow的项目解释器，Windows优先.venv/Scripts/python.exe，macOS/Linux用.venv/bin/python。所有真实生图由助手在当前会话调用原生image_gen，Python没有模型客户端或网络调用。

## 项目与计划

新故事新目录，保留原始脚本source/，助手填写plan.json（格式见demo/plan.example.json）。任务包含id、filename、mode(generate/edit/external)、完整prompt、refs(已有或计划的任务ID)、inputs(项目内原始图片相对路径)。mode=edit的首项引用是编辑源。命名保留脚本的精确PNG文件名，非法/冲突说明问题，不静默改名。外部提帧记external，原始来源写prompt，不调用生图。引用形成无环依赖。

```text
python scripts/project.py --project projects/my-story init
python scripts/project.py --project projects/my-story import --plan projects/my-story/plan.json
python scripts/project.py --project projects/my-story status
python scripts/project.py --project projects/my-story batch --id batch-001 --count 3 --authorized
python scripts/project.py --project projects/my-story next --tool-ready
```

--authorized仅当用户已授权这次准确数量/范围时带；--tool-ready仅工具实际可用时带。next原子占用一次派发，只有新的call_image_tool允许一次调用；重复next只恢复原token，不能再次绘图。传prompt和所有references真实路径，调用前核对SHA未变。超过当前参考容量会在派发前拒绝。

```text
python scripts/project.py --project projects/my-story complete --id IMG001 --token 原token --image 原生工具实际返回的PNG路径
```

保存字节原样复制，Pillow仅验证可解码与尺寸，绝不自动补边/重画。图片已落盘而事务失败时仍可用原token和同图complete恢复，不能换新job。调用成功后立刻complete，再实际目视检查；有问题review --id ... --result fail --note 具体问题会暂停原批。检查通过可review pass，但不表示客户已经批准。

## 批次与恢复

batch初次固定候选任务列表及count上限；后续追加任务不会混入旧批。--items ID1 ID2可准确限定范围，特别是局部修订。重复batch同ID不重置次数，变更count/items拒绝。计数来自持久jobs记录，不是易被冲坏的独立累加字段。所有失败/未知尝试仍保守计数。

到batch_limit停止，下次明确继续新批用新ID/count。未到数且仅用户说继续时先恢复原批；done表示本批没有待做项，blocked表示存在未满足依赖，不等于整项目完成，用status核对剩余列表。status --verify才遍历核对所有保存图片；正常next只校验本次必要参考，减少历史I/O。

断线后status/next只返回recover_original及原id/token，先找回实际原结果。必要时uncertain --id ... --token ... --note ...记录原因。不支持Python轮询原生工具。只有确认原工具根本没有接收请求时才resolve --confirm-not-submitted --note 证据；不会退还原调用计数。用户另行授权重试后retry --id ... --request-id 稳定ID将failed置为pending，再建新批。重复retry请求不重置正在运行任务，未知结果不允许retry。

外部真实提取图用external --id ... --image 实际PNG --note 真实视频提帧来源，0次新调用。绝不让生成图冒充视频尾帧。

## 版本修订

```text
python scripts/project.py --project projects/my-story revise --source IMG001 --request-id fix-001 --mode edit --prompt "只把外套改红，保留其他内容"
python scripts/project.py --project projects/my-story batch --id fix-batch-001 --count 1 --items IMG001-v2 --authorized
python scripts/project.py --project projects/my-story next --tool-ready
```

真实生成后照常complete。regenerate无需修改理由，可省prompt沿用源图的完整累计要求，不覆盖旧图。--without-reference仅明确不参考原图时用于regenerate。源任务可选历史版本；family决定版本号递增，parent记录实际源版本。修改要提供具体变化且不能省源图。为防重复，revise的request-id同内容重放返回原任务，改内容拒绝。

账本只存在项目内部state.sqlite3，无主目录全局状态；所有output/refs路径在项目内，迁移整个项目后可继续。SQLite默认回滚日志与FULL同步，dispatch和次数是同事务；关闭正在执行的进程前不要复制数据库，需在线备份时使用SQLite backup能力而非单独复制半写文件。不要用旧progress.json覆盖此账本。

## 画幅与发布检查

脚本有明确画幅时，交付前运行status --verify --aspect-ratio 9:16（替换为实际要求）。按已保存的真实像素检查，允许1像素舍入；不符时会报告aspect_ratio_match=false，不自动裁剪、补边或多生一次。原生工具不一定严格输出提示词中的画幅，发现不符用review fail保存问题，不能称尺寸已达标。

.gitignore排除实际项目、图片、视频、输入ZIP、环境和数据库；静态example-story文本demo保留。Git只排除未跟踪文件：若用户过去已跟踪过素材，先检查git ls-files，不要以为新增.gitignore会自动从历史删掉它们。没有用户明确授权不删除远程历史或强制推送。


## 暂停与时间

```text
python scripts/project.py --project projects/my-story pause --note "用户暂停"
python scripts/project.py --project projects/my-story resume
```

暂停位保存在SQLite meta，重新打开仍生效。暂停只阻止新的next，不取消已接收的原生调用；complete可以在暂停时保存晚到的真实结果。resume不改token、任务或批次计数，未知job仍recover_original。

jobs新增dispatched_at/completed_at；打开旧版本账本时通过事务增列，旧任务、用量与未知token不变，旧记录时间显示null，不编造耗时。status.timings中的dispatch_to_save_seconds是派发到保存时间，包含助手/用户停顿及工具交接；原生调用耗时要在宿主另行测量。

## CSV工具

```text
python scripts/csv_tools.py input/任务.csv --output input/任务_UTF8.csv
python scripts/csv_tools.py input/任务.csv --in-place
python scripts/csv_tools.py input/脚本.zip --in-place
```

in-place首次保留.before-encoding-fix.bak，不覆盖旧备份；新输出目标已存在则拒绝。检查全部CSV行列和值后原子替换，不执行CSV或ZIP内容。默认自动识别，必要时--encoding指定原编码；不把错误替换符静默写回。
