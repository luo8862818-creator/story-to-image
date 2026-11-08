# Story to Image 2.1.0

完整的Codex生图项目：Python管理任务与SQLite进度，ChatGPT/Codex原生工具生成图片，不接第三方或OpenAI图片API，不要求API Key。支持脚本原文件名、每次只生成N张、重新打开续作、指定版本修改与重新生成。

## 安装

需要Python 3.11+。在本目录执行（也可让Codex代为执行）：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

macOS/Linux使用python3与.venv/bin/python。唯一外部依赖Pillow用于确认图片可解码和真实尺寸；SQLite随Python提供。依赖安装时需能访问软件包源，日常管理不联网。没有原生生图工具时，终端只能管任务，不能自行出图。

用Codex打开整个仓库，然后说：

```text
按照SKILL.md，读取input里的脚本，保留规定文件名。
建立项目my-story，这次只生成3张，角色和场景也计数，下次再继续。
```

[客户使用指南](客户使用指南.md) · [任务管理命令](references/project-workflow.md) · [验证清单](验证清单.md)

## 目录

- AGENTS.md、SKILL.md、.agents/：Codex项目规则及随仓库携带的技能发现入口。
- scripts/project.py：任务管理入口，不调用图片API；csv_tools.py负责CSV/ZIP内CSV编码修复。
- demo/plan.example.json：可复制到新项目的任务样例；projects/example-story保留旧故事静态demo，不是新账本项目。
- projects/<name>/：source、plan.json、state.sqlite3、output原图，默认忽略Git提交。
- tests/test_project.py：离线状态测试，不调用生图服务。

上传GitHub时保留完整功能源码，勿提交.venv、实际客户项目/参考、数据库、密钥或成品。用Git提交时.gitignore会排除这些数据；网页手工上传仍需自行排除运行目录。源码采用MIT许可，见LICENSE。客户要继续自己的项目时应单独备份整个projects/<name>（退出执行中的命令后复制），仅克隆仓库不会带来他之前未提交的进度。不要把数据库当模板删除后重画。

## 测试

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

不需要构建发布包即可使用。上传源码前先用git ls-files审查文件，原生生图仍在本地运行。测试完成情况见[验证记录](验证记录.md)。


2.1.0补充：持久暂停/恢复、同批任一待验收项先阻止新派发、1200项依赖链迭代检查、旧账本兼容计时、稳定UTF-8 JSON输出、Excel兼容CSV修复。工程边界与实际耗时见验证记录；不保证原生服务固定速度。生成资料、CSV备份和数据库不提交GitHub。
