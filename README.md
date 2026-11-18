# 雪球专栏归档工具

如果你想把一个雪球作者的公开专栏保存下来，之后不依赖网页还能继续阅读、搜索和整理，这个工具可以把整套内容落到本地。

它会保存文章全文、原始 HTML、本地图片和文章元数据，并生成一个可以直接用浏览器打开的离线阅读册。后续同步时只补新文章；如果作者改过文章，工具会留下旧版和差异；如果文章在线上消失或变成删除提示，默认保留本地最后一版，避免归档被意外覆盖。

## 安装

如果你的 AI 或自动化宿主支持从 GitHub 接入技能，可以把这个仓库克隆到宿主配置的技能目录。技能目录的位置由宿主决定，不要直接假定某个固定路径：

```bash
git clone https://github.com/xgonggong/xueqiu-archive.git <技能目录>/xueqiu-archive
```

安装后，目录根部应当直接看到 `SKILL.md`，而不是多套一层 `xueqiu-archive/xueqiu-archive/`。可以用下面的命令检查：

```bash
test -f <技能目录>/xueqiu-archive/SKILL.md
python <技能目录>/xueqiu-archive/scripts/archive.py --help
```

Windows PowerShell 可以把第一条检查命令写成：

```powershell
Test-Path "<技能目录>\xueqiu-archive\SKILL.md"
```

如果不能使用 Git，也可以从 GitHub 下载 ZIP 后解压；最终目录结构保持一致即可。更新已安装的技能时运行：

```bash
git -C <技能目录>/xueqiu-archive pull --ff-only
```

安装技能不会自动抓取任何文章。安装完成后，用户还需要明确提出归档、同步或校验请求，宿主再按 `SKILL.md` 的流程调用脚本。

## 两种使用方式

这个项目既可以当作普通 Python 工具运行，也可以作为一个技能目录交给支持 `SKILL.md` 约定的 AI 或自动化宿主使用。

### 直接运行脚本

进入本目录后，直接执行 `scripts/archive.py`。这种方式适合定时任务、批处理或你希望自己控制每一步的场景。完整参数和工作流见 [SKILL.md](SKILL.md)。

```bash
python scripts/archive.py --uid <雪球用户UID> --author "<作者昵称>" \
  --root "<归档目录>" --missing-only
```

### 作为技能使用

把整个 `xueqiu-archive/` 目录放进技能宿主配置的技能目录，并保持下面的结构不变：

```text
xueqiu-archive/
├── SKILL.md
├── scripts/archive.py
└── references/
```

`SKILL.md` 是技能入口，里面的 `description` 用来说明什么时候应该调用它；`scripts/archive.py` 负责实际抓取、同步和自检。宿主可以根据用户的自然语言请求选择对应流程，例如：

- “把这个雪球作者的专栏归档到指定目录。”
- “同步上次归档后发布的新文章。”
- “检查这份归档有没有改文、缺图或摘要问题。”

技能本身不依赖某一个特定模型或平台。宿主负责理解用户请求、确定 UID 和输出目录，并按 `SKILL.md` 的流程调用脚本；脚本仍然是独立可执行的 Python 程序。

## 适合什么时候使用

- 想长期保存某个雪球作者的公开专栏
- 定期同步新文章，查看作者是否修改过旧文
- 把文章整理成可搜索的本地资料库
- 在原始全文之上，另外维护摘要、划重点和观点台账
- 检查已有归档是否缺文件、缺图片或出现内容污染

它只处理公开可见内容，不尝试绕过登录、付费、粉丝权限或反爬限制。

## 环境要求

- Python 3，脚本只使用标准库，不需要额外安装 Python 包
- 首次归档、增量同步和改文检测需要网络；`--verify`、`--pending-summaries` 和 `--check-summaries` 可以离线运行
- 下面的多行示例使用 Bash 的 `\` 续行写法；在 PowerShell 中可以把命令写成一行，或把 `\` 换成反引号 `` ` ``

## 快速开始

先用少量页面做一次连通性检查：

```bash
python scripts/archive.py \
  --uid <雪球用户UID> \
  --author "<作者昵称>" \
  --root "<归档目录>" \
  --meta-only --max-pages 3
```

确认接口可用后，开始首次归档：

```bash
python scripts/archive.py \
  --uid <雪球用户UID> \
  --author "<作者昵称>" \
  --root "<归档目录>" \
  --missing-only
```

归档完成后做一次离线自检：

```bash
python scripts/archive.py \
  --root "<归档目录>" \
  --verify
```

以后同步同一个作者时，可以直接复用已有元数据：

```bash
python scripts/archive.py \
  --root "<归档目录>" \
  --missing-only
```

如果需要检查作者是否改过文章，可以先运行 `--check-edits`；确认有变化后，再运行一次不带 `--missing-only` 的全量同步，工具会保存旧版和 diff。

## 归档目录里有什么

```text
归档目录/
├── index.html              浏览器离线阅读册，可搜索和按主题筛选
├── 目录.csv                文章索引
├── 观点台账.csv            后续整理观点和验证结果的模板
├── metadata.json           作者、文章和抓取状态
├── 变更日志.md             每轮同步记录
├── articles/               Markdown 全文和原始 HTML
├── images/                 正文配图的本地副本
├── summaries/              可选的摘要文件
└── _snapshots/             改文、重建和删改前的旧版本
```

`index.html` 可以单独打开阅读；如果希望图片也正常显示，需要把同级的 `images/` 目录一起保留。真正需要长期备份的源数据主要是 `articles/`、`images/` 和 `summaries/`。

## 摘要和划重点

摘要是独立的后处理层，不会改变原始全文。把摘要按 `summaries/<文章ID>.md` 保存后，重新运行一次 `--missing-only`，它就会出现在阅读册对应文章的顶部。

批量处理摘要前，先运行：

```bash
python scripts/archive.py --root "<归档目录>" --pending-summaries
```

它会列出哪些文章需要新写或重写摘要，正文没有变化的文章可以跳过。摘要写完后，用下面的命令检查链接、发布时间、原文基线和数字引用：

```bash
python scripts/archive.py --root "<归档目录>" --check-summaries --stamp
```

摘要中的数字和观点仍需回到原文人工核对；自动检查只能发现格式、来源和部分数字对应关系。

## 目录说明

- `SKILL.md`：完整工作流、参数说明和数据保护规则
- `scripts/archive.py`：归档、同步、生成阅读册和离线自检脚本
- `references/site-notes.md`：雪球接口、限流和常见异常记录
- `references/summary-guide.md`：摘要文件格式与复查规范

## 使用边界

归档用于个人研究和离线阅读，原文著作权归作者。文章中的行情、财务数据和投资判断是作者的内容，引用或据此决策前应回到原文核实；本工具不构成投资建议。
