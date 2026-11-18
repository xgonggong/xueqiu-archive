# -*- coding: utf-8 -*-
"""
雪球专栏通用本地全文归档 / 增量同步脚本（Skill: xueqiu-archive）

把一个作者的专栏长文抓成一份保真的本地原始数据库，后续摘要、台账、
检索等加工都基于这份原始数据，不重复抓取。

用法:
    python archive.py --uid <UID> --author <昵称> --root <输出目录> --missing-only
    python archive.py --uid <UID> --root <输出目录> --verify
    python archive.py --uid <UID> --root <输出目录> --pending-summaries
    python archive.py --uid <UID> --root <输出目录> --check-summaries --stamp
    python archive.py --uid <UID> --root <输出目录> --check-edits
    python archive.py --uid <UID> --root <输出目录> --meta-only --max-pages 3

参数:
    --uid          雪球用户 ID。已有归档可省略（自动从 metadata.json 恢复）；
                   首次归档必须提供，或设 XQ_UID 环境变量，否则报错退出
    --author       作者昵称，仅用于文件头与阅读册标题；已有归档可省略
    --root         归档输出目录，默认 ./xueqiu-<UID>
    --max-pages    时间线最多扫多少页，默认 90；小规模测试可设 3
    --reads-file   可选 JSON，注入 {"文章ID": "阅读量"}（接口不提供该数字）
    --topics-file  可选 JSON，覆盖主题分类规则 [[主题, [关键词...]], ...]

模式:
    --missing-only   日常增量：找新文章并补抓，已有归档不动（推荐）
    (无模式参数)      全量重扫 + 重抓，检测作者改文并生成旧版 diff
    --rebuild        联网重抓并按新 HTML 覆盖，不生成改文 diff
    --verify         纯离线自检，不联网（含摘要复查硬错误 + 磁盘体积报告）
    --pending-summaries  纯离线列出需要新写/重写摘要的篇目；正文 md5 与基线
                         一致的文章直接跳过，不重复读全文（不写任何文件）
    --check-summaries  纯离线复查 summaries/*.md，生成「摘要复查报告.md」
    --stamp          必须配合 --check-summaries：把当前正文 md5 写成摘要基线
    --accept-edits   配合 --check-summaries --stamp：原文已变时，确认已按新
                     正文复核摘要后才允许更新基线（否则拒绝盖章并判失败）
    --strict         严格模式：任何待复核项（含数字未命中）一律判失败，交付门禁

互斥校验：--stamp 单独使用、--accept-edits 缺 --stamp、多个检查模式并存、
--missing-only + --rebuild 会直接报错退出（避免「看似只读的参数」意外触发
全量抓取与覆盖写盘）。「正文未变不重新总结」由 source-md5 基线驱动：脚本
不调用大模型，token 只花在把待办篇目的全文读进上下文这一步。
    --accept-shrink  显式接受正文大幅缩水（确认作者主动删改，不是占位符）
    --check-edits    只比对 edited_at / 标题，不抓正文

删文 / 占位符保护：线上文章不可见或正文疑似空壳时，一律保留本地 md 与
原始 HTML 快照、把旧正文喂回阅读册，并在 metadata.json 标记原因，不覆盖。
判断只看相对变化（与上一份 .source.html 同口径比较），不设绝对字数门槛。
默认主文件就是最后一版完整全文；只有 --accept-shrink 放行才会覆盖，
覆盖前额外留 _snapshots/<ID>_<时间>.before-delete.md 并把路径写进元数据、
目录与阅读册，--verify 会校验它存在。--rebuild 覆盖前另存 .pre-rebuild.md。
    --meta-only      只扫元数据
    --skip-scan      复用已有元数据，不重新扫时间线

产出（全部落在 --root 下）:
    metadata.json / articles/<ID>.md / articles/<ID>.source.html / images/
    _snapshots/ / 变更日志.md / 目录.csv / 观点台账.csv / index.html
    summaries/<ID>.md（可选）/ 摘要复查报告.md（跑过复查才有）

边界: 仅个人研究存档，著作权归原作者；只抓公开可见内容，内置节流与
限流降级，保留作者与原文链接，禁止公开分发。详见 references/site-notes.md。
"""
import os
import re
import sys
import csv
import json
import time
import html
import hashlib
import difflib
import datetime
import urllib.request
import http.cookiejar
from html.parser import HTMLParser
from urllib.parse import urlsplit

# 以下常量由 configure() 在运行时按命令行参数覆盖；默认值仅用于兼容旧归档
ROOT = os.getcwd()
ARTICLES = os.path.join(ROOT, 'articles')
IMAGES = os.path.join(ROOT, 'images')
SNAPSHOTS = os.path.join(ROOT, '_snapshots')
UID = ''
AUTHOR = ''
BASE = 'https://xueqiu.com'
COLUMN_URL = BASE + '/' + UID + '/column'
MAX_PAGES = 90
TZ = datetime.timezone(datetime.timedelta(hours=8))

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0 Safari/537.36',
    'Accept': 'text/html,application/json;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': BASE + '/',
}

# 时间线接口 view_count 恒为 0；如要保留专栏页上看到的数字，用 --reads-file 注入
# 运行时会先从已有 metadata.json 恢复这些数字，避免增量同步把它们清空
READS = {}

# 归档专有字段：由抓取流程写入，时间线接口不提供，增量同步时必须从旧元数据继承
ARCHIVE_FIELDS = ('fetched_at', 'body_chars', 'images', 'source', 'topic', 'reads',
                  'last_full_snapshot', 'shrink_note')
# 状态字段：--missing-only 完全复用本地快照时不能把「仅本地存档」悄悄洗成「正常」。
# 不含 remote_missing —— 它由 ID 差集补回逻辑每轮显式重算，继承会让它永远清不掉。
STATE_FIELDS = ('local_snapshot_reason', 'last_seen')

# 通用投资类主题词，不绑定任何特定作者的标题；具体作者可用 --topics-file 定制
TOPIC_RULES = [
    ('科技与AI', ['AI', '人工智能', '算力', '芯片', '半导体', '光模块', '存储',
               '服务器', '大模型', '数据中心', '机器人', '光芯片', 'PCB', '液冷']),
    ('行业与个股', ['产业链', '上游', '下游', '个股', '公司', '财报', '业绩', '营收',
                '龙头', '竞争格局', '赛道', '行业', '商业模式', '估值']),
    ('牛熊与周期', ['牛市', '熊市', '周期', '反弹', '底部', '顶部', '出清', '跌幅',
                '趋势', '拐点', '择时', '抄底', '赔率', '牛转熊']),
    ('宏观与政策', ['宏观', '美联储', '降息', '利率', '通胀', '汇率', '政策', '地缘',
                '财政', '货币', '黄金', '原油', '美元', '选举']),
    ('体系与方法论', ['体系', '方法论', '交易系统', '框架', '认知', '纪律', '仓位',
                 '心态', '修行', '原则', '选股', '复盘', '哲学']),
    ('随笔与总结', ['随笔', '感悟', '年度', '总结', '随想', '杂谈', '读书', '问答', '来信']),
]


def validate_options(p, opts):
    """参数互斥与无效组合一律显式报错/提示，避免静默忽略后给出假「通过」。

    最危险的是 --stamp 单独使用：它原本只是一行提示，随后会一路走到全量
    抓取并覆盖 articles/、metadata.json、index.html。这里直接拦死。
    """
    checks = [(n, on) for n, on in (('--verify', opts.verify),
                                    ('--check-summaries', opts.check_summaries),
                                    ('--check-edits', opts.check_edits),
                                    ('--pending-summaries', opts.pending_summaries)) if on]
    if opts.stamp and not opts.check_summaries:
        p.error('--stamp 必须配合 --check-summaries 使用'
                '（单独使用会触发全量抓取与覆盖写盘）')
    if opts.accept_edits and not (opts.check_summaries and opts.stamp):
        p.error('--accept-edits 必须配合 --check-summaries --stamp 使用'
                '（它的唯一作用是：确认已按新正文复核后，才允许更新原文基线）')
    if len(checks) > 1:
        p.error('检查模式一次只能用一个：%s' % '、'.join(n for n, _ in checks))
    if opts.missing_only and opts.rebuild:
        p.error('--missing-only 与 --rebuild 互斥（--rebuild 会全量重抓）')
    if checks:
        for flag, on in (('--missing-only', opts.missing_only),
                         ('--rebuild', opts.rebuild), ('--skip-scan', opts.skip_scan),
                         ('--meta-only', opts.meta_only),
                         ('--accept-shrink', opts.accept_shrink)):
            if on:
                print('  ! %s 在 %s 模式下不生效，已忽略'
                      % (flag, checks[0][0]))
    elif opts.strict:
        print('  ! --strict 只在 --verify / --check-summaries 下生效，本次忽略')
    if opts.meta_only and (opts.skip_scan or opts.accept_shrink):
        print('  ! --meta-only 只扫元数据，同步类参数不会生效')


def parse_options():
    import argparse
    p = argparse.ArgumentParser(description='雪球专栏本地全文归档')
    p.add_argument('--uid', default=os.environ.get('XQ_UID', ''))
    p.add_argument('--author', default=os.environ.get('XQ_AUTHOR', ''))
    p.add_argument('--root', default=os.environ.get('XQ_ROOT', ''))
    p.add_argument('--max-pages', type=int, default=MAX_PAGES)
    p.add_argument('--reads-file', default='')
    p.add_argument('--topics-file', default='')
    for flag in ('--missing-only', '--skip-scan', '--rebuild', '--verify',
                 '--check-edits', '--meta-only'):
        p.add_argument(flag, action='store_true')
    p.add_argument('--check-summaries', action='store_true',
                   help='纯离线复查摘要，生成「摘要复查报告.md」')
    p.add_argument('--stamp', action='store_true',
                   help='配合 --check-summaries：把当前正文 md5 写进摘要作为基线')
    p.add_argument('--accept-edits', action='store_true',
                   help='配合 --check-summaries --stamp：确认已按新正文复核摘要后，允许更新基线')
    p.add_argument('--pending-summaries', action='store_true',
                   help='纯离线列出需要新写/重写摘要的文章；原文未变的直接跳过')
    p.add_argument('--strict', action='store_true',
                   help='严格模式：任何待复核项（含数字未命中）一律判为失败')
    p.add_argument('--accept-shrink', action='store_true',
                   help='显式接受正文大幅缩水（确认是作者主动删改而非占位符）')
    opts = p.parse_args()
    validate_options(p, opts)
    return opts


def read_local_metadata(root_hint=''):
    """读取已有归档的 metadata.json，用于恢复 uid / author / 阅读量等归档专有信息。"""
    candidates = []
    if root_hint:
        # 显式指定/推导出的 --root：只认该目录，绝不回退到 cwd。
        # 否则在归档 A 里对空目录 B 首次归档会误继承 A 的 uid/author/reads。
        candidates.append(os.path.join(root_hint, 'metadata.json'))
    else:
        # 仅「在归档目录内直接运行、不传 --root」时才靠 cwd 恢复身份
        candidates.append(os.path.join(os.getcwd(), 'metadata.json'))
    for mp in candidates:
        if os.path.exists(mp):
            try:
                return json.load(open(mp, encoding='utf-8')), os.path.dirname(mp)
            except Exception as e:
                print('  ! 已有 metadata.json 解析失败，忽略：%s' % e)
    return {}, None


def configure(opts, create_dirs=True):
    """按命令行参数重写全局配置；已有归档自动恢复 uid / author / 阅读量。"""
    global ROOT, ARTICLES, IMAGES, SNAPSHOTS, UID, AUTHOR, COLUMN_URL
    global MAX_PAGES, READS, TOPIC_RULES

    uid = str(opts.uid or '')
    root = (os.path.abspath(opts.root) if opts.root
            else os.path.join(os.getcwd(), 'xueqiu-' + uid) if uid else '')
    disk, disk_root = read_local_metadata(root)
    disk_uid = str(disk.get('uid') or '')
    if uid and disk_uid and uid != disk_uid:
        raise SystemExit(
            'UID 冲突：指定了 %s，但 %s 下已有归档属于 %s。'
            '请确认 --root 与 --uid 指向同一作者；要归档新作者请换一个 --root。'
            % (uid, root, disk_uid))
    uid = uid or disk_uid
    if not uid:
        if (opts.verify or opts.check_summaries or opts.check_edits
                or opts.pending_summaries):
            raise SystemExit(
                '无法执行检查：%s 下没有可用的 metadata.json，无法恢复作者身份。\n'
                '请确认 --root 指向一份已有归档；检查模式不会创建目录或抓取。' % root)
        raise SystemExit(
            '缺少作者 UID：首次归档请传 --uid <UID>（或设 XQ_UID）；已有归档请把 '
            '--root 指向归档目录，脚本会自动从 metadata.json 恢复，不会误抓其他作者。')
    if not root:
        root = disk_root or os.path.join(os.getcwd(), 'xueqiu-' + uid)

    UID = uid
    AUTHOR = opts.author or disk.get('author') or ('UID ' + uid)
    COLUMN_URL = BASE + '/' + UID + '/column'
    MAX_PAGES = max(1, opts.max_pages)
    # 已有归档时，过小的 --max-pages 会把索引截断成部分文章；忽略该限制。
    # 只在真的会扫时间线的模式下打印，否则会给出误导性的「按 90 页全量扫描」。
    will_scan = not (opts.verify or opts.check_summaries or opts.check_edits
                     or opts.pending_summaries or opts.skip_scan)
    if (disk.get('articles') and will_scan and not opts.meta_only
            and MAX_PAGES < 90):
        print('  ! 已有归档：忽略 --max-pages %d，按 90 页全量扫描，避免索引被截断'
              % MAX_PAGES)
        MAX_PAGES = 90
    ROOT = root
    ARTICLES = os.path.join(ROOT, 'articles')
    IMAGES = os.path.join(ROOT, 'images')
    SNAPSHOTS = os.path.join(ROOT, '_snapshots')
    if create_dirs:
        for d in (ARTICLES, IMAGES, SNAPSHOTS, os.path.join(ROOT, 'summaries')):
            os.makedirs(d, exist_ok=True)

    # 阅读量只有专栏列表页展示，公开接口拿不到；先从已有归档继承，再用文件覆盖
    READS = {aid: str(a.get('reads') or '')
             for aid, a in (disk.get('articles') or {}).items() if a.get('reads')}
    if opts.reads_file:
        if not os.path.exists(opts.reads_file):
            print('  ! 阅读量文件不存在，忽略：%s' % opts.reads_file)
        else:
            try:
                READS.update(json.load(open(opts.reads_file, encoding='utf-8')))
            except Exception as e:
                print('  ! 阅读量文件解析失败，忽略：%s' % e)
    if opts.topics_file:
        if not os.path.exists(opts.topics_file):
            print('  ! 主题规则文件不存在，使用默认规则：%s' % opts.topics_file)
        else:
            try:
                TOPIC_RULES = [tuple(x) for x in json.load(
                    open(opts.topics_file, encoding='utf-8'))]
            except Exception as e:
                print('  ! 主题规则解析失败，使用默认规则：%s' % e)
    print('配置：作者 %s（UID %s）· 输出 %s · 时间线最多 %d 页'
          % (AUTHOR, UID, ROOT, MAX_PAGES))


def topic_of(title):
    for name, kws in TOPIC_RULES:
        if any(k in title for k in kws):
            return name
    return '其他'


def topic_of_meta(m):
    """已有分类保持不变，新文章才按当前规则分类，避免规则升级后整库主题突变。"""
    return m.get('topic') or topic_of(m['title'])


def merge_archive_fields(new_meta, old_meta):
    """时间线每轮重建 dict；把归档专有字段从旧元数据继承回来，防止增量同步抹掉。"""
    n = 0
    for aid, m in new_meta.items():
        old = old_meta.get(aid) or {}
        for key in ARCHIVE_FIELDS + STATE_FIELDS:
            if not m.get(key) and old.get(key):
                m[key] = old[key]
                n += 1
    return n


def now_str():
    return datetime.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')


def ts_str(ms):
    if not ms:
        return ''
    return datetime.datetime.fromtimestamp(ms / 1000, TZ).strftime('%Y-%m-%d %H:%M')


# ---------------------------------------------------------------- 网络层
COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


def warm():
    try:
        OPENER.open(urllib.request.Request(BASE + '/', headers=UA), timeout=20).read()
    except Exception:
        pass


def get(url, expect=None, retries=3):
    """带 WAF 识别与重试的 GET。expect 为期望出现的字符串。"""
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            data = OPENER.open(req, timeout=25).read().decode('utf-8', 'ignore')
            if expect and expect not in data:
                raise RuntimeError('unexpected content (WAF or empty page)')
            return data
        except Exception as e:
            if i == retries - 1:
                print('    ! 抓取失败 %s (%s)' % (url[-60:], type(e).__name__))
                return None
            time.sleep(0.9 * (i + 1))
            if i == 1:
                warm()
    return None


# ---------------------------------------------------------------- 正文转换
class MarkdownConverter(HTMLParser):
    """把雪球正文 HTML 转成干净的 Markdown。"""
    BLOCK = {'p', 'div', 'figure', 'blockquote', 'ul', 'ol', 'li', 'table', 'tr',
             'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'section'}

    def __init__(self, image_prefix, image_map):
        super().__init__(convert_charrefs=True)
        self.buf = []
        self.hrefs = []
        self.list_stack = []
        self.image_prefix = image_prefix
        self.image_map = image_map
        self.skip = 0

    def nl(self, n=1):
        tail = ''.join(self.buf[-8:])
        cur = 0
        for ch in reversed(tail):
            if ch == '\n':
                cur += 1
            else:
                break
        if cur < n:
            self.buf.append('\n' * (n - cur))

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag in self.BLOCK:
            self.nl(2 if tag in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'figure') else 1)
        if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.buf.append('#' * int(tag[1]) + ' ')
        elif tag in ('strong', 'b'):
            self.buf.append('**')
        elif tag in ('em', 'i'):
            self.buf.append('*')
        elif tag == 'a':
            self.hrefs.append(a.get('href', ''))
            self.buf.append('[')
        elif tag == 'img':
            src = a.get('src', '')
            name = self.image_map.get(src)
            target = (self.image_prefix + name) if name else src
            self.nl()
            self.buf.append('![%s](%s)' % (a.get('alt') or '配图', target))
            self.nl()
        elif tag == 'br':
            self.buf.append('\n')
        elif tag == 'hr':
            self.buf.append('---\n\n')
        elif tag in ('ul', 'ol'):
            self.list_stack.append([tag, 0])
        elif tag == 'li':
            if self.list_stack:
                self.list_stack[-1][1] += 1
                kind, num = self.list_stack[-1]
                self.buf.append('- ' if kind == 'ul' else '%d. ' % num)
        elif tag == 'blockquote':
            self.buf.append('> ')
        elif tag == 'tr':
            self.buf.append('| ')
        elif tag in ('td', 'th'):
            pass

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ('strong', 'b'):
            self.buf.append('**')
        elif tag in ('em', 'i'):
            self.buf.append('*')
        elif tag == 'a':
            href = self.hrefs.pop() if self.hrefs else ''
            self.buf.append('](%s)' % href)
        elif tag in ('ul', 'ol'):
            if self.list_stack:
                self.list_stack.pop()
            self.nl(2)
        elif tag in ('td', 'th'):
            self.buf.append(' | ')
        elif tag == 'tr':
            self.nl()
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'figure', 'blockquote', 'li'):
            self.nl(2)

    def handle_data(self, data):
        if not self.skip:
            self.buf.append(data)

    def result(self):
        s = ''.join(self.buf)
        s = re.sub(r'[ \t]+\n', '\n', s)
        s = re.sub(r'\n{3,}', '\n\n', s)
        return s.strip() + '\n'


SAFE_HTML_TAGS = {
    'a', 'blockquote', 'br', 'code', 'del', 'div', 'em', 'figcaption',
    'figure', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'li',
    'ol', 'p', 'pre', 's', 'span', 'strong', 'table', 'tbody', 'td', 'tfoot',
    'th', 'thead', 'tr', 'u', 'ul',
}
SAFE_HTML_VOID_TAGS = {'br', 'hr', 'img'}
BLOCKED_HTML_TAGS = {
    'embed', 'form', 'iframe', 'math', 'object', 'script', 'style', 'svg',
    'template',
}
SAFE_HTML_ATTRS = {
    '*': {'title'},
    'a': {'href', 'target'},
    'img': {'alt', 'height', 'src', 'width'},
    'td': {'colspan', 'rowspan'},
    'th': {'colspan', 'rowspan'},
}


def safe_url(value, kind='href'):
    """Return a URL safe for the generated offline reader, or an empty string.

    Article HTML and summary files are treated as untrusted input.  In
    particular, merely escaping quotes does not stop ``javascript:`` URLs.
    """
    value = html.unescape(str(value or '')).replace('\x00', '').strip()
    if not value:
        return ''
    probe = re.sub(r'[\x00-\x20]+', '', value).lower()
    if probe.startswith(('javascript:', 'vbscript:', 'data:')):
        return ''
    if value.startswith('//'):
        value = 'https:' + value
    scheme = urlsplit(value).scheme.lower()
    if scheme:
        allowed = {'http', 'https'}
        if kind == 'href':
            allowed.add('mailto')
        if scheme not in allowed:
            return ''
    elif not (value.startswith(('#', '/', './', '../'))
              or not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value)):
        return ''
    return value


class SafeHTMLParser(HTMLParser):
    """Allowlist renderer for untrusted article HTML embedded in index.html."""

    def __init__(self, image_map):
        super().__init__(convert_charrefs=True)
        self.image_map = image_map or {}
        self.out = []
        self.blocked_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_HTML_TAGS:
                self.blocked_depth += 1
            return
        if tag in BLOCKED_HTML_TAGS:
            self.blocked_depth = 1
            return
        if tag not in SAFE_HTML_TAGS:
            return
        attrs = dict(attrs)
        cleaned = []
        for name, value in attrs.items():
            name = name.lower()
            if name.startswith('on') or name not in SAFE_HTML_ATTRS.get('*', set()) | SAFE_HTML_ATTRS.get(tag, set()):
                continue
            value = value or ''
            if name == 'src':
                value = self.image_map.get(value) or self.image_map.get(html.unescape(value)) or value
                if value in self.image_map.values():
                    value = 'images/' + value
                value = safe_url(value, 'src')
            elif name == 'href':
                value = safe_url(value, 'href')
            elif name == 'target' and value not in ('_blank', '_self', '_parent', '_top'):
                continue
            if not value and name in ('href', 'src'):
                continue
            cleaned.append('%s="%s"' % (name, html.escape(value, quote=True)))
        if tag == 'a' and any(x.startswith('target="_blank"') for x in cleaned):
            cleaned.append('rel="noopener noreferrer"')
        self.out.append('<%s%s>' % (tag, (' ' + ' '.join(cleaned)) if cleaned else ''))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.blocked_depth:
            if tag in BLOCKED_HTML_TAGS:
                self.blocked_depth -= 1
            return
        if tag in SAFE_HTML_TAGS and tag not in SAFE_HTML_VOID_TAGS:
            self.out.append('</%s>' % tag)

    def handle_data(self, data):
        if not self.blocked_depth:
            self.out.append(html.escape(data))

    def handle_comment(self, data):
        # Comments are unnecessary in the reader and could hide active markup.
        return

    def result(self):
        return ''.join(self.out)


def clean_html(text_html, image_map):
    """Generate safe HTML for index.html, with images mapped to local files."""
    parser = SafeHTMLParser(image_map)
    try:
        parser.feed(text_html or '')
        parser.close()
    except Exception:
        # A malformed article should still produce readable escaped text.
        return '<p>%s</p>' % html.escape(plain(text_html or ''))
    return parser.result()


def plain(text_html):
    s = re.sub(r'<[^>]+>', ' ', text_html)
    return re.sub(r'\s+', ' ', html.unescape(s)).strip()


def norm_body(s):
    """比较正文时忽略排版空白与图片引用，避免因抓取通道不同误报改文。"""
    s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)
    return re.sub(r'\s+', '', s)


# 线上文章被删除 / 改成占位符时，绝不能用空壳覆盖本地快照
PLACEHOLDER_WORDS = (
    '该内容已被删除', '内容已被删除', '文章已删除', '帖子已删除', '已被作者删除',
    '该文章不存在', '文章不存在', '内容不存在', '页面不存在', '此内容不可见',
    '该内容不可见', '内容已下架', '该帖已被', '内容违规', 'not found', '404',
    '访问频繁', '请稍后再试', '系统繁忙', '服务不可用',
)


def md_text_len(s):
    """把 Markdown 正文折算成接近 HTML 纯文本的字数（仅在缺原始 HTML 快照时兜底）。"""
    s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)          # 图片
    s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)       # 链接只留文字，丢掉 URL
    s = re.sub(r'<[^>]+>', ' ', s)
    return len(re.sub(r'[\s#*>`|_~]', '', s))


def body_stats(text_html, prev_md='', prev_html=''):
    """返回 (线上纯文本字数, 上一版纯文本字数)。只测量，不做任何长度判定。

    优先用上一份 .source.html 的原始 HTML 做底本：两侧都是 HTML 纯文本，口径完全一致，
    否则 Markdown 里的链接 URL、图片引用会让旧正文虚高，把正常的重抓误判成「缩水」。
    """
    n = len(re.sub(r'\s+', '', plain(text_html)))
    if prev_html:
        old_n = len(re.sub(r'\s+', '', plain(prev_html)))
    elif prev_md:
        old_n = md_text_len(prev_md.split('\n---\n\n', 1)[-1])
    else:
        old_n = 0
    return n, old_n


def body_quality(text_html, prev_md='', accept_shrink=False, prev_html=''):
    """判断新抓正文是否可信，返回 (是否可写盘, 原因)。

    不设任何绝对字数门槛——专栏里本来就有几百字的短文，一刀切会误伤。
    只保留三条与「相对变化」有关的规则：
      1. 完全没有文字（空响应 / 提取失败）永远不写盘；
      2. 命中删除/占位关键词，且相对上一版明显变短（首次归档无参照时一律拒绝）；
      3. 相对上一版缩到不足一半。
    比较底本优先取上一份 .source.html，与本次抓到的 HTML 同口径。
    确认是作者主动删改时用 --accept-shrink 放行 2、3；空文本不在放行范围内。
    """
    txt = re.sub(r'\s+', '', plain(text_html))
    if not txt:
        return False, '正文没有任何文字（空响应或提取失败）'
    if accept_shrink:
        return True, ''
    n, old_n = body_stats(text_html, prev_md, prev_html)
    low = txt.lower()
    hit = next((w for w in PLACEHOLDER_WORDS if w.lower() in low), '')
    # 长文里偶尔出现「404」之类字样不该被误判，所以关键词必须与「明显变短」同时成立；
    # 首次归档没有可比的上版快照，关键词命中就直接拒绝。
    if hit and (not (prev_md or prev_html) or (old_n and n < old_n * 0.9)):
        return False, ('正文命中删除/占位关键词「%s」，且相对本地正文明显变短'
                       '（%d → %d 字）' % (hit, old_n, n))
    if old_n and n < old_n * 0.5:
        return False, ('正文从约 %d 字缩到 %d 字（不足一半），疑似被替换成占位符'
                       % (old_n, n))
    return True, ''


def save_snapshot(aid, prev_md, stamp, tag=''):
    """把上一版正文存进 _snapshots/，返回相对路径；同一轮同标签不重复写。

    tag 为空时保持历史命名 <ID>_<时间>.md；pre-rebuild / before-delete 用于标明留档原因。
    """
    rel = '_snapshots/%s_%s%s.md' % (aid, stamp, ('.' + tag) if tag else '')
    path = os.path.join(ROOT, rel)
    os.makedirs(SNAPSHOTS, exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(prev_md)
    return rel


def reuse_local_body(aid, prev_md, raw_path, bodies, article_html, article_imgs):
    """线上取不到可信正文时，把本地快照喂回本轮输出，保证索引与阅读册不丢条目。"""
    bodies[aid] = prev_md.split('\n---\n\n', 1)[-1]
    if os.path.exists(raw_path):
        article_html[aid] = open(raw_path, encoding='utf-8').read()
        article_imgs[aid] = recover_image_map(aid, article_html[aid])
    # 没有原始 HTML 时不要把 aid 塞进 article_html，让 build_outputs 回退到 Markdown 渲染


def auto_judgement(md_body):
    """从正文中摘录疑似核心判断，供观点台账预填（需人工核对）。"""
    txt = re.sub(r'[#*>\[\]()!|`-]', ' ', md_body)
    txt = re.sub(r'\s+', ' ', txt)
    hits = []
    for sent in re.split(r'(?<=[。！？；])', txt):
        sent = sent.strip()
        if 12 < len(sent) < 180 and any(k in sent for k in
                                        ('结论', '一句话', '我认为', '核心', '总结', '本质是', '意味着')):
            hits.append(sent)
        if len(hits) >= 2:
            break
    return ' / '.join(hits)[:300]


# ---------------------------------------------------------------- 扫描
def scan_timeline(max_pages=None):
    """遍历用户时间线，收集所有专栏长文（is_column + title 非空）。"""
    if max_pages is None:
        max_pages = MAX_PAGES
    found = {}
    page = 1
    while page <= max_pages:
        url = '%s/statuses/user_timeline.json?user_id=%s&page=%d&count=20' % (BASE, UID, page)
        data = get(url, expect='"statuses"')
        if not data or not data.lstrip().startswith('{'):
            print('  page %d 无法解析，停止扫描' % page)
            break
        try:
            j = json.loads(data)
        except Exception:
            print('  page %d JSON 解析失败，停止扫描' % page)
            break
        sts = j.get('statuses') or []
        if not sts:
            break
        for s in sts:
            if s.get('is_column') and s.get('title'):
                aid = str(s['id'])
                found[aid] = {
                    'id': aid,
                    'title': html.unescape(re.sub(r'<[^>]+>', '', s['title'])).strip(),
                    'created_at': s.get('created_at'),
                    'edited_at': s.get('edited_at'),
                    'published': ts_str(s.get('created_at')),
                    'edited': ts_str(s.get('edited_at')),
                    'description': plain(s.get('description') or ''),
                    'fav_count': s.get('fav_count', 0),
                    'like_count': s.get('like_count', 0),
                    'reply_count': s.get('reply_count', 0),
                    'retweet_count': s.get('retweet_count', 0),
                    'reads': READS.get(aid, ''),
                    'url': '%s/%s/%s' % (BASE, UID, aid),
                }
        if page % 10 == 0:
            print('  已扫描 %d 页，累计专栏文章 %d 篇' % (page, len(found)))
        page += 1
        time.sleep(0.12)
    print('  时间线扫描完成：%d 页，共 %d 篇专栏文章' % (page - 1, len(found)))
    return found


# ---------------------------------------------------------------- 抓正文
WEB_BLOCKED = False


def recover_image_map(aid, text_html):
    """复用已存正文时，重建 src -> 本地图片文件名映射。"""
    srcs = [s for s in dict.fromkeys(
        re.findall(r'<img[^>]*?src=["\']([^"\']+)["\']', text_html))
        if 'xqimg.imedao.com' in s]
    out = {}
    for i, src in enumerate(srcs, 1):
        name = '%s_%d%s' % (aid, i, '.png' if '.png' in src else '.jpg')
        if os.path.exists(os.path.join(IMAGES, name)):
            out[src] = name
    return out


def md_inline(s):
    s = html.escape(s)
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)

    def link_repl(m):
        url = safe_url(html.unescape(m.group(2)), 'href')
        if not url:
            # Keep the visible label while dropping an unsafe destination.
            return m.group(1)
        return '<a href="%s" rel="noopener noreferrer">%s</a>' % (
            html.escape(url, quote=True), m.group(1))

    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link_repl, s)
    return s


def md_to_html(body):
    """把已归档的 Markdown 正文还原成阅读册可用的 HTML 片段。"""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith('#'):
            n = min(len(s) - len(s.lstrip('#')), 5)
            out.append('<h%d>%s</h%d>' % (n, md_inline(s.lstrip('# ').strip()), n))
            continue
        im = re.match(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$', s)
        if im:
            src = safe_url(im.group(2).replace('../images/', 'images/'), 'src')
            if not src:
                out.append('<p>%s</p>' % html.escape(im.group(1)))
                continue
            out.append('<figure><img src="%s" alt="%s" /></figure>'
                       % (html.escape(src, quote=True), html.escape(im.group(1), quote=True)))
            continue
        if s.startswith('---'):
            out.append('<hr/>')
        elif s.startswith('> '):
            out.append('<blockquote>%s</blockquote>' % md_inline(s[2:]))
        else:
            out.append('<p>%s</p>' % md_inline(s))
    return '\n'.join(out)


SUMMARY_BEGIN = '<!-- summary:begin -->'
SUMMARY_END = '<!-- summary:end -->'


def summary_md_to_html(text):
    """把 summaries/*.md 转成阅读册摘要卡 HTML（标题/表格/列表/引用/粗体/链接）。"""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith('<!--') or re.match(r'^-{3,}$', s):
            i += 1
            continue
        if s.startswith('|'):                       # 表格
            block = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                block.append(lines[i].strip())
                i += 1
            rows = [[c.strip() for c in r.strip('|').split('|')] for r in block]
            rows = [r for r in rows if not (
                r and all(re.match(r'^:?-{2,}:?$', c or '') for c in r))]
            if rows:
                buf = ['<table class="hl-table"><thead><tr>']
                buf += ['<th>%s</th>' % md_inline(c) for c in rows[0]]
                buf.append('</tr></thead>')
                if len(rows) > 1:
                    buf.append('<tbody>')
                    for r in rows[1:]:
                        buf.append('<tr>' + ''.join(
                            '<td>%s</td>' % md_inline(c) for c in r) + '</tr>')
                    buf.append('</tbody>')
                buf.append('</table>')
                out.append(''.join(buf))
            continue
        hm = re.match(r'^(#{1,6})\s+(.*)$', s)      # 标题（H1 由阅读册提供）
        if hm:
            n = len(hm.group(1))
            if n > 1:
                tag = 'h4' if n == 2 else 'h5'
                out.append('<%s>%s</%s>' % (tag, md_inline(hm.group(2)), tag))
            i += 1
            continue
        if s.startswith('>'):                       # 引用块
            block = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                block.append(lines[i].strip().lstrip('>').strip())
                i += 1
            out.append('<blockquote>%s</blockquote>'
                       % '<br/>'.join(md_inline(x) for x in block))
            continue
        if re.match(r'^(\d+\.|[-*])\s+', s):        # 有序 / 无序列表
            tag = 'ol' if re.match(r'^\d+\.', s) else 'ul'
            block = []
            while i < len(lines) and re.match(r'^(\d+\.|[-*])\s+', lines[i].strip()):
                block.append(re.sub(r'^(\d+\.|[-*])\s+', '', lines[i].strip()))
                i += 1
            out.append('<%s>%s</%s>' % (
                tag, ''.join('<li>%s</li>' % md_inline(x) for x in block), tag))
            continue
        out.append('<p>%s</p>' % md_inline(s))
        i += 1
    return '\n'.join(out)


def load_summary_html(aid):
    """读取该篇划重点摘要；不存在则返回空串，阅读册照常只显示全文。"""
    path = os.path.join(ROOT, 'summaries', aid + '.md')
    if not os.path.exists(path):
        return ''
    text = open(path, encoding='utf-8').read()
    if SUMMARY_BEGIN in text:
        text = text.split(SUMMARY_BEGIN, 1)[1]
    if SUMMARY_END in text:
        text = text.split(SUMMARY_END, 1)[0]
    return summary_md_to_html(text)


def fetch_article(meta):
    """主域文章页优先（含完整配图）；被限流时降级到 api 子域单篇接口。"""
    global WEB_BLOCKED
    aid = meta['id']
    text_html = ''
    source = 'web'

    def has_text(h):
        # 只判断「有没有文字」，不再用 HTML 长度阈值：短文、纯图注都可能是合法正文
        return bool(re.sub(r'\s+', '', plain(h)))

    if not WEB_BLOCKED:
        data = get(meta['url'], expect='SNOWMAN_STATUS', retries=1)
        if data:
            mm = re.search(r'window\.SNOWMAN_STATUS\s*=\s*(\{.*?\});', data, re.S)
            if mm:
                try:
                    text_html = json.loads(mm.group(1)).get('text') or ''
                except Exception:
                    pass
            if not has_text(text_html):
                dm = re.search(r'<div class="article__bd__detail">(.*?)</article>', data, re.S)
                text_html = dm.group(1) if dm else ''
        else:
            WEB_BLOCKED = True
            print('  · 主域触发限流，后续改用 api 子域通道')

    if not has_text(text_html):
        api = get('https://api.xueqiu.com/statuses/show.json?id=%s' % aid,
                  expect='"text"', retries=2)
        if api and api.lstrip().startswith('{'):
            try:
                text_html = json.loads(api).get('text') or ''
                source = 'api'
            except Exception:
                pass

    if not has_text(text_html):
        print('    ! %s 正文获取失败' % aid)
        return None

    if source == 'api' and 'xqimg.imedao.com' not in text_html:
        # 不再从镜像页补图：镜像页常混入站点广告图，宁可缺图也要保证快照是纯净原文
        print('    · %s 接口未返回配图，按纯文字归档' % aid)

    # 图片本地化（只处理正文配图，表情图保留外链）
    srcs = list(dict.fromkeys(re.findall(r'<img[^>]*?src=["\']([^"\']+)["\']', text_html)))
    image_map = {}
    idx = 0
    for src in srcs:
        if 'xqimg.imedao.com' not in src:
            continue
        idx += 1
        ext = '.png' if '.png' in src else '.jpg'
        name = '%s_%d%s' % (aid, idx, ext)
        path = os.path.join(IMAGES, name)
        if not os.path.exists(path):
            url = src if src.startswith('http') else 'https:' + src
            raw = get_binary(url)
            if raw:
                with open(path, 'wb') as f:
                    f.write(raw)
        if os.path.exists(path):
            image_map[src] = name

    return {'text_html': text_html, 'image_map': image_map,
            'page_title': meta['title'], 'raw_len': len(text_html), 'source': source}


def get_binary(url, retries=2):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, Referer=BASE + '/'))
            raw = OPENER.open(req, timeout=25).read()
            head = raw[:200].lstrip().lower()
            if len(raw) > 500 and not head.startswith(
                    (b'<!doctype', b'<html', b'<script', b'<textarea')):
                return raw
        except Exception:
            pass
        time.sleep(0.5)
    return None


def build_markdown(meta, art):
    conv = MarkdownConverter('../images/', art['image_map'])
    conv.feed(art['text_html'])
    body = conv.result()
    header = (
        '# %s\n\n'
        '- 作者：%s（雪球专栏）\n'
        '- 原文：%s\n'
        '- 发布：%s（北京时间）\n'
        '- 修改：%s（北京时间）\n'
        '- 归档：%s（抓取时间戳见 metadata.json）\n\n'
        '---\n\n'
    ) % (meta['title'], AUTHOR, meta['url'], meta['published'],
         meta['edited'] or meta['published'], now_str())
    return header + body, body


# ---------------------------------------------------------------- 输出
def write_csv(path, header, rows):
    try:
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
    except PermissionError:
        # 文件被 Excel / 编辑器占用时不中断整轮同步，保留原有文件
        print('  ! %s 正被其他程序占用，本次跳过写入（关闭该文件后重跑即可）'
              % os.path.basename(path))


def archive_status(m, has_body=True):
    """该篇的线上状态，用于目录 CSV 与阅读册提示；不猜测是删除还是接口未覆盖。"""
    if m.get('shrink_note'):
        return str(m['shrink_note'])[:120]
    reason = m.get('local_snapshot_reason')
    if reason:
        return '仅本地存档：' + str(reason)[:40]
    if m.get('remote_missing'):
        return '线上本轮未覆盖（本地快照保留）'
    return '正常' if has_body else '抓取失败'


def build_outputs(meta_all, bodies, changed_log, article_html, article_imgs):
    all_ordered = sorted(meta_all.values(), key=lambda x: x['created_at'] or 0, reverse=True)
    ordered = [m for m in all_ordered if m['id'] in bodies]

    write_csv(os.path.join(ROOT, '目录.csv'),
              ['序号', '文章ID', '标题', '发布日期', '最后修改', '主题', '阅读量(雪球专栏页)',
               '点赞', '评论', '转发', '摘要', '原文链接', '本地文件', '是否已被修改过',
               '全文状态', '线上状态'],
              [[i, m['id'], m['title'], m['published'], m['edited'], topic_of_meta(m),
                m['reads'], m['like_count'], m['reply_count'], m['retweet_count'],
                m['description'][:200], m['url'], 'articles/%s.md' % m['id'],
                '是' if m['edited'] and m['edited'] != m['published'] else '',
                '已归档' if m['id'] in bodies else '抓取失败',
                archive_status(m, m['id'] in bodies)]
               for i, m in enumerate(all_ordered, 1)])

    write_csv(os.path.join(ROOT, '观点台账.csv'),
              ['文章ID', '标题', '发布日期', '主题', '核心判断(自动摘录,需核对)',
               '成立条件(待填)', '关键标的/数字(待填)', '后续是否兑现(待填)', '原文链接'],
              [[m['id'], m['title'], m['published'], topic_of_meta(m),
                auto_judgement(bodies.get(m['id'], '')), '', '', '', m['url']]
               for m in ordered])

    # ---------------- index.html
    esc = html.escape
    items = []
    docs = []
    n_sum = 0
    for i, m in enumerate(ordered, 1):
        aid, title = m['id'], m['title']
        topic = topic_of_meta(m)
        if aid in article_html:
            frag = clean_html(article_html[aid], article_imgs.get(aid, {}))
        else:
            frag = md_to_html(bodies.get(aid, ''))
        if not frag.strip():
            frag = '<p>正文缺失</p>'
        hi = load_summary_html(aid)
        if hi:
            n_sum += 1
            hi_html = ('<div class="highlight"><div class="highlight-hd">'
                       '<span class="hl-tag">划重点</span>'
                       '<a class="hl-jump" href="#body%s">↓ 跳到全文</a>'
                       '</div>%s</div>' % (aid, hi))
            no_hi = ''
        else:
            hi_html = ''
            no_hi = ('<p class="no-summary">本篇暂无划重点摘要；'
                     '取消左侧「只看划重点」即可阅读全文。</p>')
        status = archive_status(m, aid in bodies)
        if status == '正常':
            local_note = ''
        elif m.get('shrink_note') and m.get('last_full_snapshot'):
            # 作者主动删改后，让「删除前最后一版全文」在阅读册里一点就能打开
            local_note = (' · <span class="local-note">⚠ %s'
                          ' <a href="%s">打开删除前全文</a></span>'
                          % (esc(status), esc(m['last_full_snapshot'])))
        else:
            local_note = ' · <span class="local-note">⚠ %s</span>' % esc(status)
        docs.append(
            '<section class="doc" id="doc%s"><h2>%s</h2>'
            '<div class="doc-meta">%s 发布 · %s · '
            '<a href="%s" target="_blank">查看原文</a>%s</div>%s%s'
            '<div class="article-body" id="body%s">%s</div></section>'
            % (aid, esc(title), esc(m['published']), esc(topic), esc(m['url']),
               local_note, hi_html, no_hi, aid, frag))
        summary = esc(m['description'][:110])
        star = ' <span class="hl-star" title="已生成划重点摘要">★</span>' if hi else ''
        items.append(
            '<button class="item" data-id="%s" data-title="%s" data-summary="%s" '
            'data-topic="%s" data-date="%s">'
            '<span class="it-date">%s</span>'
            '<span class="it-title">%s%s</span>'
            '<span class="it-meta">%s%s</span>'
            '<span class="it-sum">%s</span></button>'
            % (aid, esc(title), summary, esc(topic), esc(m['published']),
               esc(m['published'][:10]), esc(title), star, esc(topic),
               (' · ' + esc(m['reads']) + ' 阅读') if m['reads'] else '', summary))

    topics = sorted(set(topic_of_meta(m) for m in ordered))
    opts = ''.join('<option value="%s">%s</option>' % (esc(t), esc(t)) for t in topics)

    page = HTML_SHELL % {
        'author': esc(AUTHOR),
        'total': len(ordered),
        'nsum': n_sum,
        'updated': esc(now_str()),
        'column': esc(COLUMN_URL),
        'items': '\n'.join(items),
        'docs': '\n'.join(docs),
        'opts': opts,
        'recent': esc(ordered[0]['published'][:10]) if ordered else '',
        'oldest': esc(ordered[-1]['published'][:10]) if ordered else '',
    }
    with open(os.path.join(ROOT, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page)

    # 无变更的轮次不再追加「无新增或改动」空段：日志会无限膨胀且毫无信息量，
    # 本轮同步时间已记录在 metadata.json 的 last_sync 里。
    if changed_log:
        with open(os.path.join(ROOT, '变更日志.md'), 'a', encoding='utf-8') as f:
            f.write('\n## 同步 %s\n' % now_str())
            for line in changed_log:
                f.write('- %s\n' % line)
    return len(ordered)


HTML_SHELL = u'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(author)s的雪球专栏 · 离线归档</title>
<style>
:root{--bg:#f7f8fa;--panel:#ffffff;--ink:#1d2430;--muted:#6b7684;--line:#e3e7ed;--accent:#b3352c;--soft:#f0f3f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.85 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 26px;position:sticky;top:0;z-index:5}
h1{margin:0;font-size:19px;letter-spacing:.3px}
.sub{color:var(--muted);font-size:13px;margin-top:5px}
.sub a{color:var(--accent)}
.wrap{display:flex;align-items:flex-start;min-height:calc(100vh - 90px)}
aside{width:340px;flex:0 0 340px;background:var(--panel);border-right:1px solid var(--line);height:calc(100vh - 90px);overflow-y:auto;position:sticky;top:90px}
.search{padding:14px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel);z-index:2}
.search input,.search select{width:100%%;padding:9px 11px;border:1px solid var(--line);border-radius:6px;background:var(--soft);color:var(--ink);font-size:13px;outline:none}
.search select{margin-top:8px}
.stat{padding:8px 14px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line)}
.item{display:block;width:100%%;text-align:left;border:0;border-bottom:1px solid var(--line);background:transparent;padding:12px 15px;cursor:pointer;color:var(--ink);font:inherit}
.item:hover{background:var(--soft)}
.item.active{background:#fdf1ef;border-left:3px solid var(--accent);padding-left:12px}
.it-date{font-size:11px;color:var(--muted);display:block}
.it-title{font-size:14px;font-weight:600;display:block;margin:2px 0;line-height:1.5}
.it-meta{font-size:11px;color:var(--accent);display:block}
.it-sum{font-size:12px;color:var(--muted);display:block;margin-top:4px;line-height:1.55;max-height:38px;overflow:hidden}
main{flex:1;min-width:0;padding:26px 40px 80px;max-width:980px}
#reader-title{font-size:23px;line-height:1.45;margin:0 0 6px}
#reader-meta{color:var(--muted);font-size:12px;margin-bottom:22px;padding-bottom:14px;border-bottom:1px solid var(--line)}
#reader{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:30px 34px;min-height:400px}
#reader p{margin:0 0 1.05em}
#reader h1,#reader h2,#reader h3,#reader h4,#reader h5{line-height:1.5;margin:1.6em 0 .7em;font-size:17px}
#reader strong{color:var(--accent)}
#reader a{color:#1a5fb4;text-decoration:none;border-bottom:1px dotted #9ab}
#reader img{max-width:100%%;height:auto;display:block;margin:14px auto;border:1px solid var(--line);border-radius:4px}
#reader figure{margin:14px 0}
#reader table{border-collapse:collapse;width:100%%;margin:14px 0}
#reader td,#reader th{border:1px solid var(--line);padding:6px 9px}
#reader blockquote{border-left:3px solid var(--line);margin:14px 0;padding-left:14px;color:var(--muted)}
.doc{display:block;border-top:1px solid var(--line);padding-top:20px;margin-top:28px}
.doc:first-child{border-top:0;padding-top:0;margin-top:0}
.doc>h2{font-size:19px;line-height:1.5;margin:0 0 4px}
.doc>.doc-meta{color:var(--muted);font-size:12px;margin:0 0 18px;padding-bottom:12px;border-bottom:1px solid var(--line)}
body.js-on .doc{display:none}
body.js-on .doc.active{display:block}
body.js-on #reader-title,body.js-on #reader-meta{display:none}
.note{background:#fff8e8;border:1px solid #f0dfae;color:#6a5320;padding:10px 14px;border-radius:6px;font-size:12px;margin-bottom:20px}
#reader .highlight{background:#f5f8ff;border:1px solid #cddcff;border-left:5px solid #2f5bd7;border-radius:8px;padding:18px 22px;margin:0 0 26px}
#reader .highlight-hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
#reader .hl-tag{background:#e3ecff;color:#1d4ed8;font-size:12px;font-weight:700;padding:3px 9px;border-radius:4px;letter-spacing:.5px}
#reader .hl-jump{font-size:12px;color:#1a5fb4}
#reader .highlight h4{font-size:15px;color:#1d4ed8;margin:18px 0 8px;border:0;padding:0;line-height:1.5}
#reader .highlight h5{font-size:14px;margin:14px 0 6px;line-height:1.5}
#reader .highlight p{font-size:14px;line-height:1.75;margin:0 0 .7em}
#reader .highlight>blockquote:first-of-type{background:#fff;border:1px solid #dce7ff;border-left:3px solid #2f5bd7;border-radius:6px;padding:12px 15px;margin:0 0 12px;color:#1c2430;font-size:15px;line-height:1.7}
#reader .highlight blockquote{margin:10px 0;padding-left:12px;color:var(--muted);font-size:12.5px}
#reader .highlight strong{color:#1d4ed8}
#reader .hl-table{border-collapse:collapse;width:100%%;margin:12px 0;font-size:13px}
#reader .hl-table th,#reader .hl-table td{border:1px solid #dbe4f5;padding:7px 10px;vertical-align:top;text-align:left}
#reader .hl-table th{background:#eaf0ff;font-weight:700;color:#26354f}
#reader .highlight ul,#reader .highlight ol{margin:0 0 .8em;padding-left:20px}
#reader .highlight li{font-size:14px;line-height:1.7;margin-bottom:5px}
#reader .no-summary{display:none;background:#fff8e8;border:1px solid #f0dfae;color:#6a5320;padding:10px 14px;border-radius:6px;font-size:13px}
#reader .local-note{color:#9a5b13;font-weight:600}
body.summary-only .article-body{display:none}
body.summary-only #reader .no-summary{display:block}
body.summary-only #reader .hl-jump{display:none}
.hl-star{color:#b3352c;font-size:12px;margin-left:2px}
.only-summary{display:flex;align-items:center;gap:6px;margin-top:9px;font-size:12.5px;color:#3b4759;cursor:pointer;white-space:nowrap}
.only-summary input{accent-color:#b3352c;margin:0}
@media(max-width:820px){.wrap{display:block}aside{width:100%%;height:auto;position:static;border-right:0}main{padding:20px}}
</style>
</head>
<body>
<header>
  <h1>%(author)s的雪球专栏 · 本地全文归档</h1>
  <div class="sub">共 %(total)s 篇 · %(oldest)s ～ %(recent)s · 归档更新于 %(updated)s ·
    <a href="%(column)s" target="_blank">专栏原址</a> · 仅供个人研究，著作权归原作者</div>
</header>
<div class="wrap">
  <aside>
    <div class="search">
      <input id="q" type="search" placeholder="搜索标题、摘要或全文…">
      <select id="topic"><option value="">全部主题</option>%(opts)s</select>
      <label class="only-summary"><input type="checkbox" id="only-summary"> 只看划重点（%(nsum)s 篇）</label>
    </div>
    <div class="stat">显示 <b id="count">%(total)s</b> / %(total)s 篇　·　点击左侧任一文章在此阅读</div>
    <div id="list">%(items)s</div>
  </aside>
  <main>
    <h2 id="reader-title">选择一篇文章开始阅读</h2>
    <div id="reader-meta">左侧支持全文检索与主题筛选</div>
    <div class="note">正文为抓取当时的快照。文中的行情数字、观点与判断均属作者个人整理，非一手信源，引用前请核实原文；本归档不构成投资建议。<br/>已为 <b>%(nsum)s</b> 篇生成「划重点」摘要，显示在正文顶部（左侧目录带 ★ 标记）；勾选「只看划重点」可隐藏全文。</div>
    <article id="reader">%(docs)s</article>
  </main>
</div>
<script>
(function(){
  document.body.classList.add('js-on');
  var items = Array.prototype.slice.call(document.querySelectorAll('.item'));
  var docs = Array.prototype.slice.call(document.querySelectorAll('.doc'));
  var q = document.getElementById('q');
  var topic = document.getElementById('topic');
  var countEl = document.getElementById('count');
  function apply(){
    var kw = q.value.trim().toLowerCase();
    var tp = topic.value;
    var n = 0;
    items.forEach(function(it){
      var hit = (!kw || it._hay.indexOf(kw) >= 0) && (!tp || it.getAttribute('data-topic') === tp);
      it.style.display = hit ? '' : 'none';
      if (hit) { n++; }
    });
    countEl.textContent = n;
  }
  items.forEach(function(it){
    var d = document.getElementById('doc' + it.getAttribute('data-id'));
    it._hay = (it.getAttribute('data-title') + ' ' + it.getAttribute('data-summary') + ' ' +
               (d ? d.textContent : '')).toLowerCase();
    it.addEventListener('click', function(){
      items.forEach(function(x){ x.classList.remove('active'); });
      it.classList.add('active');
      docs.forEach(function(x){ x.classList.remove('active'); });
      if (d) { d.classList.add('active'); }
      document.getElementById('reader-title').textContent = it.getAttribute('data-title');
      document.getElementById('reader-meta').textContent = it.getAttribute('data-date') + ' 发布 · ' + it.getAttribute('data-topic');
      window.scrollTo(0, 0);
    });
  });
  q.addEventListener('input', apply);
  topic.addEventListener('change', apply);
  var onlySum = document.getElementById('only-summary');
  onlySum.addEventListener('change', function(){
    document.body.classList.toggle('summary-only', onlySum.checked);
  });
  var first = items.filter(function(x){ return x.querySelector('.hl-star'); })[0] || items[0];
  if (first) { first.click(); }
})();
</script>
</body>
</html>
'''


# ------------------------------------------------------------ 划重点摘要复查
# 摘要是加工层，最容易出的错：数字抄错 / 把别篇的数据写进来 / 原文改了摘要没跟上 /
# md 改了但阅读册没重建。能自动判定的做成硬错误，需要人判断的整理成待核清单。
STAMP_MD5_RE = re.compile(r'<!--\s*source-md5:\s*([0-9a-fA-F]{32})\s*-->\s*')
STAMP_DATE_RE = re.compile(r'<!--\s*reviewed:[^>]*-->\s*')
UNIT_PAT = r'(?:%|倍|点|成|万亿|亿|万|bp|BP|个月|美元|元)'
NUM_PAT = r'\d[\d,]*(?:\.\d+)?'
# 注意：全角逗号「，」不映射到半角逗号。中文语境里它出现在「2763，0.382」这类
# 并列数字之间，转成半角后会被 NUM_PAT 的千分位规则吞成「27630.382」，凭空造出不存在的数字。
FULL2HALF = str.maketrans('０１２３４５６７８９．％', '0123456789.%')

# 严格限定的点分日期：仅 19xx/20xx 开头、1-2 位月、可选 1-2 位日。
# 只剥离日期本身，不动其他数字；在 numbers_in_summary / source_text_of 里调用。
DOT_DATE_RE = re.compile(r'(?<![0-9.])(?:19|20)\d{2}\.\d{1,2}(?:\.\d{1,2})?(?![0-9.])')


def strip_dot_dates(s):
    """把 2024.9.24 / 2015.6.5 / 2011.09 这类点分日期替换成空格。"""
    return DOT_DATE_RE.sub(' ', s)


def norm_digits(s):
    """全角数字/百分号转半角，去掉同一行内数字与数字/百分号之间的排版空格。

    不跨换行删空格：否则行末「2.64」会与下一行章节号「5.」粘成「2.645」。
    跨行的点分日期已由调用方先 strip_dot_dates 剥离，不会因空格保留而误判。
    """
    s = s.translate(FULL2HALF)
    return re.sub(r'(?<=\d)[ \t]+(?=[\d%])', '', s)


def expand_ranges(s):
    """把「13 到 18%」「3到5个月」补成「13% 到 18%」「3个月 到 5个月」。

    摘要和原文都做同样展开，范围两端才能各自带单位命中，而不是退化成裸数字。
    """
    return re.sub(r'(' + NUM_PAT + r')\s*(?:到|至|~|-)\s*(' + NUM_PAT
                  + r')\s*(' + UNIT_PAT + r')',
                  lambda m: '%s%s 到 %s%s' % (m.group(1), m.group(3),
                                              m.group(2), m.group(3)), s)


def summary_body(text):
    """截取 summary:begin/end 之间的正文；标记缺失或顺序错误时返回空串。"""
    if SUMMARY_BEGIN not in text or SUMMARY_END not in text:
        return ''
    if text.index(SUMMARY_BEGIN) > text.index(SUMMARY_END):
        return ''
    return text.split(SUMMARY_BEGIN, 1)[1].split(SUMMARY_END, 1)[0]


def source_text_of(aid):
    """合并 Markdown 正文与原始 HTML 快照的纯文本，作为数字溯源底本。"""
    parts = []
    p = os.path.join(ARTICLES, aid + '.md')
    if os.path.exists(p):
        parts.append(open(p, encoding='utf-8').read().split('\n---\n', 1)[-1])
    p = os.path.join(ARTICLES, aid + '.source.html')
    if os.path.exists(p):
        parts.append(plain(open(p, encoding='utf-8').read()))
    joined = '\n'.join(parts)
    # 先剥离点分日期，避免 norm_digits 把「2015.6.5 4037.96」粘成假数字
    return expand_ranges(norm_digits(strip_dot_dates(joined)))


def body_hash_of(aid):
    """正文（去头部/空白/图片引用）的 md5，用作摘要写作时的原文基线。"""
    p = os.path.join(ARTICLES, aid + '.md')
    if not os.path.exists(p):
        return ''
    body = open(p, encoding='utf-8').read().split('\n---\n', 1)[-1]
    return hashlib.md5(norm_body(body).encode('utf-8')).hexdigest()


def numbers_in_summary(text):
    """抽取摘要里需要回原文核对的数字，过滤章节号、日期、链接、长 ID。"""
    s = norm_digits(strip_dot_dates(text))
    s = re.sub(r'<!--.*?-->', ' ', s, flags=re.S)
    s = re.sub(r'https?://\S+', ' ', s)
    s = re.sub(r'`[^`]*`', ' ', s)                       # 行内代码（本地路径）
    s = re.sub(r'(?m)^\s*#{1,6}\s*\d+[｜.|、]\s*', '', s)  # ## 1｜ 章节号
    s = re.sub(r'(?m)^\s*\d+\.\s+', '', s)                # 1. 列表序号
    s = re.sub(r'\b\d{4}(?:/\d{4})+\b', ' ', s)           # 2015/2018/2012
    # 「月」作为第三段时要求带「日」字结尾，否则「2000年3月5132点」会被误吃成
    # 日期只剩「32点」；「2026年10月29日」正常剥离。斜杠/连字符日期不受影响。
    s = re.sub(r'\b(?:19|20)\d{2}[-/年]\d{1,2}(?:[-/.]\d{1,2}|月\d{1,2}日)?', ' ', s)
    s = re.sub(r'\b\d{1,2}/\d{1,2}\b', ' ', s)            # 6/25、10/28
    s = re.sub(r'\b\d{1,2}:\d{2}\b', ' ', s)              # 10:55
    s = expand_ranges(s)                                   # 13 到 18% → 13% 到 18%
    out, seen = [], set()
    for m in re.finditer(r'(?<![\d.])(' + NUM_PAT + r')\s*(' + UNIT_PAT + r')?', s):
        num = m.group(1).replace(',', '')
        unit = (m.group(2) or '').strip()
        if (num, unit) in seen:
            continue
        seen.add((num, unit))
        # 月份/编号/年份/长 ID 这类数字到处都有，命中也没有辨识度
        weak = not unit and (len(num) <= 2 or len(num) >= 8
                             or bool(re.match(r'^(?:19|20)\d{2}$', num)))
        out.append({'num': num, 'unit': unit, 'weak': weak,
                    'display': num + unit})
    return out


# 同一单位在原文里的另一种写法，避免「25bp」被误报成查无此数
UNIT_EQUIV = {'bp': ('个基点', '基点'), 'BP': ('个基点', '基点'),
              '点': ('个点',), '%': ('个百分点',)}


def find_number(src, num, unit):
    """返回 (等级, 原文片段)：exact 带单位一致 / numeric 仅数字命中 / none 未找到。"""
    variants = [num]
    if re.match(r'^\d+\.0+$', num):
        variants.append(num.split('.')[0])
    elif '.' in num and num.endswith('0'):
        variants.append(num.rstrip('0').rstrip('.'))
    forms = ([unit] + list(UNIT_EQUIV.get(unit, ()))) if unit else []

    def context(m):
        return re.sub(r'\s+', ' ',
                      src[max(0, m.start() - 26):m.end() + 26]).strip()

    for v in variants:
        for u in forms:                       # 带单位（含等价写法）
            m = re.search(r'(?<![\d.])' + re.escape(v + u) + r'(?![\d.])', src)
            if m:
                return ('exact', context(m))
        m = re.search(r'(?<![\d.])' + re.escape(v) + r'(?![\d.])', src)
        if m:
            return ('numeric', context(m))
    return ('none', '')


PENDING_LABELS = {
    'changed': '原文已变，必须按新正文重写',
    'invalid': '摘要标记损坏，需修复',
    'no_source': '本地全文缺失，需先补抓',
    'baseline': '待补原文基线（通常不必重读全文）',
    'new': '还没有摘要，需要读全文新写',
}
PENDING_HINT = {
    'changed': '正文 md5 与摘要基线不同；旧摘要可能过期，必须按新正文重写。',
    'invalid': 'summary:begin/end 标记缺失或为空；按现有内容补好标记即可。',
    'no_source': '先跑 --missing-only 补回正文，再谈摘要。',
    'baseline': '摘要已写好，只是没记录基线；跑一次 --check-summaries --stamp 补上，'
                '核对报告即可，一般不用把全文再读一遍。',
    'new': '只读这些篇目的全文，写完 summaries/<文章ID>.md。',
}


def pending_summary_items():
    """纯离线：按原文基线把文章分成「需要加工」与「可跳过」两类。

    判断依据只有 summaries/<ID>.md 的 <!-- source-md5: ... --> 与当前正文 md5。
    脚本本身不调用任何 LLM；token 只花在把全文读进上下文这一步，因此清单
    越精确越省。原文未变的文章一律归入 skip，不要重新总结。
    """
    mp = os.path.join(ROOT, 'metadata.json')
    meta = json.load(open(mp, encoding='utf-8')).get('articles', {})
    todo, skip = [], []
    for aid, m in sorted(meta.items(),
                         key=lambda kv: kv[1].get('created_at') or 0,
                         reverse=True):
        cur = body_hash_of(aid)
        sp = os.path.join(ROOT, 'summaries', aid + '.md')
        if not cur:
            todo.append((aid, m, 'no_source', '找不到 articles/%s.md' % aid))
        elif not os.path.exists(sp):
            todo.append((aid, m, 'new', '尚无摘要'))
        else:
            text = open(sp, encoding='utf-8').read()
            if not summary_body(text).strip():
                todo.append((aid, m, 'invalid', '摘要标记缺失或为空'))
            else:
                old = STAMP_MD5_RE.search(text)
                if not old:
                    todo.append((aid, m, 'baseline', '摘要存在但无基线'))
                elif old.group(1).lower() != cur:
                    todo.append((aid, m, 'changed', '原文已变化 %s → %s'
                                 % (old.group(1)[:8], cur[:8])))
                else:
                    skip.append((aid, m, cur))
    return todo, skip


def run_pending_summaries():
    """打印增量摘要待办清单；不写任何文件，避免又多一份可再生产物。"""
    mp = os.path.join(ROOT, 'metadata.json')
    if not os.path.isdir(ROOT) or not os.path.exists(mp):
        print('待办查询失败：%s 不是一份已有归档（缺少 metadata.json）。' % ROOT)
        print('请确认 --root 指向归档目录；本模式纯离线，不会创建目录。')
        return False
    todo, skip = pending_summary_items()
    print('摘要待办（纯离线）—— 共 %d 篇：需处理 %d 篇 · 可跳过 %d 篇'
          % (len(todo) + len(skip), len(todo), len(skip)))
    print('判定依据：summaries/<ID>.md 的 source-md5 基线 vs 当前正文 md5。'
          '脚本不调用大模型，token 只花在读全文上。')
    if skip:
        print('⏭ 可跳过的 %d 篇正文与摘要基线一致：不要重新读全文、不要重写摘要。'
              % len(skip))
    for key in ('changed', 'invalid', 'no_source', 'baseline', 'new'):
        rows = [x for x in todo if x[2] == key]
        if not rows:
            continue
        print('\n【%s】%d 篇 — %s'
              % (PENDING_LABELS[key], len(rows), PENDING_HINT[key]))
        for aid, m, _key, why in rows:
            print('  %-11s %-30s articles/%s.md  %s'
                  % (aid, (m.get('title') or '（无标题）')[:28], aid, why))
    if skip:
        print('\n【可跳过】%d 篇 — 正文未变，本轮完全不用碰' % len(skip))
        print('  ' + ' '.join(aid for aid, _m, _h in skip))
    print('\n处理顺序：只把上面「需处理」篇目的全文读入上下文 → 写/改 '
          'summaries/<ID>.md → --check-summaries --stamp → 核对报告 → '
          '--missing-only → --verify')
    if not todo:
        print('✓ 没有待办：全部摘要都与原文基线一致，本轮无需任何总结动作。')
    return True


def audit_summaries(write_report=True, stamp=False, strict=False, accept_edits=False):
    """纯离线复查所有 summaries/*.md，返回汇总 dict；可写「摘要复查报告.md」。

    strict=True 时把「待人工复核」项一律升级为硬错误，用于批量交付前的门禁。
    """
    import glob
    sdir = os.path.join(ROOT, 'summaries')
    files = sorted(glob.glob(os.path.join(sdir, '*.md')))
    meta = {}
    mp = os.path.join(ROOT, 'metadata.json')
    if os.path.exists(mp):
        try:
            meta = json.load(open(mp, encoding='utf-8')).get('articles', {})
        except Exception:
            pass
    page_path = os.path.join(ROOT, 'index.html')
    page = open(page_path, encoding='utf-8').read() if os.path.exists(page_path) else ''
    errors, warns, blocks, blocked = [], [], [], []
    n_num = n_exact = n_numeric = n_missing = 0

    for p in files:
        aid = os.path.basename(p)[:-3]
        text = open(p, encoding='utf-8').read()
        m = meta.get(aid) or {}
        per_e, per_w, lines = [], [], []
        cur_hash = body_hash_of(aid)
        prev_stamp = STAMP_MD5_RE.search(text)
        stamp_ok = stamp_blocked = False

        # --stamp：把当前正文 md5 写成基线注释；注释不进阅读册，不影响渲染。
        # 原文已变时拒绝无条件盖章，否则「摘要可能过期」会被一次 --stamp 抹掉，
        # 等于没重读全文也假装复核过。必须显式 --accept-edits 才更新。
        if stamp and cur_hash:
            stale = bool(prev_stamp and prev_stamp.group(1).lower() != cur_hash)
            if stale and not accept_edits:
                stamp_blocked = True
                blocked.append('[%s] 原文已变化（%s → %s），拒绝更新基线：'
                               '先按新正文重写/复核摘要，确认后再加 --accept-edits'
                               % (aid, prev_stamp.group(1)[:8], cur_hash[:8]))
                lines.append('- 基线：⚠ 原文已变化，未更新（需 --accept-edits）')
            else:
                cleaned = STAMP_DATE_RE.sub('', STAMP_MD5_RE.sub('', text))
                cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
                marker = ('<!-- source-md5: %s -->\n<!-- reviewed: %s -->\n'
                          % (cur_hash, now_str()))
                cleaned = (cleaned.replace(SUMMARY_BEGIN, marker + SUMMARY_BEGIN, 1)
                           if SUMMARY_BEGIN in cleaned else marker + cleaned)
                with open(p, 'w', encoding='utf-8') as f:
                    f.write(cleaned)
                text = cleaned
                stamp_ok = True
                lines.append('- 基线：%s' % (
                    '⚠ 已在 --accept-edits 下按新正文更新（%s → %s）'
                    % (prev_stamp.group(1)[:8], cur_hash[:8]) if stale
                    else '已写入当前正文 md5 与复查时间'))

        body = summary_body(text)
        if not body.strip():
            per_e.append('缺少成对的 <!-- summary:begin --> / <!-- summary:end --> '
                         '标记，或标记之间为空（阅读册不会显示卡片）')
        if not m:
            per_e.append('文章 ID 不在 metadata.json：文件名必须是 <文章ID>.md，'
                         '否则摘要永远不会显示')
        if not cur_hash:
            per_e.append('找不到该篇本地全文 articles/%s.md' % aid)

        # 张冠李戴：摘要里的原文链接必须与该篇元数据一致
        links = re.findall(r'https://xueqiu\.com/[^\s)｜|`]+', body)
        if m.get('url') and links:
            for link in links:
                if link.rstrip('/。｜') != m['url'].rstrip('/'):
                    per_e.append('摘要原文链接 %s 与元数据 %s 不符，'
                                 '可能把别的文章的数据写进来了' % (link, m['url']))
                    break
        elif m.get('url') and body:
            per_w.append('摘要未写出原文链接')
        # 发布时间与本地全文路径也必须指向本篇
        pm = re.search(r'发布[^\d]{0,12}(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', body)
        if pm and m.get('published') and pm.group(1) != m['published']:
            per_e.append('摘要写的发布时间 %s 与元数据 %s 不符'
                         % (pm.group(1), m['published']))
        for ref in set(re.findall(r'articles/(\d+)\.md', body)):
            if ref != aid:
                per_e.append('摘要里引用了别篇的全文 articles/%s.md（本篇是 %s）'
                             % (ref, aid))

        # 合规标注
        if body and ('免责声明' not in body or '不构成投资建议' not in body):
            per_e.append('缺少免责声明或「不构成投资建议」')
        if body and re.search(r'\d\s*(?:%|倍|点|成|亿|万|bp)', body):
            if '非一手' not in body or '需核实原文' not in body:
                per_w.append('含行情/财务数字，但未标注「非一手来源、需核实原文」')
        if body and '概率' in body and not ('主观判断' in body or '非测算' in body):
            per_w.append('含情景概率，但未标注「作者主观判断、非测算」')

        # 原文基线：作者改文或重建排版后，摘要必须重新复核
        old = STAMP_MD5_RE.search(text)
        if stamp_blocked:
            base_msg = '✗ 原文已变化，基线未更新（按新正文复核后加 --accept-edits）'
            per_e.append('原文已变化但本次拒绝更新基线；按新正文重写/复核摘要后，'
                         '加 --accept-edits 重新跑 --check-summaries --stamp')
        elif not old:
            base_msg = '⚠ 未记录原文基线（跑一次 --check-summaries --stamp 建立）'
            per_w.append('未记录原文基线，无法自动发现「原文改了摘要没跟上」')
        elif cur_hash and old.group(1).lower() != cur_hash:
            base_msg = '✗ 原文已变化（%s → %s）' % (old.group(1)[:8], cur_hash[:8])
            per_e.append('原文正文已与摘要写作时不同，摘要可能过期；'
                         '按新正文复核后再跑 --check-summaries --stamp --accept-edits')
        else:
            base_msg = '✅ 与摘要基线一致（%s）' % cur_hash[:8]
        if stamp_ok:
            base_msg = '✅ 本次已建立/更新基线（%s）' % cur_hash[:8]

        # 数字溯源
        src = source_text_of(aid)
        nums = [x for x in (numbers_in_summary(body) if body else []) if not x['weak']]
        n_weak = len(numbers_in_summary(body)) - len(nums) if body else 0
        rows = []
        for x in nums:
            lvl, ctx = find_number(src, x['num'], x['unit']) if src else ('none', '')
            x['level'], x['ctx'] = lvl, ctx
            n_num += 1
            if lvl == 'exact':
                n_exact += 1
            elif lvl == 'numeric':
                n_numeric += 1
            else:
                n_missing += 1
                per_w.append('数字 %s 未在原文找到，需回原文核实（可能是换算值或抄错）'
                             % x['display'])
            mark = {'exact': '✅ 带单位一致', 'numeric': '◐ 仅数字命中',
                    'none': '❌ 未找到'}[lvl]
            ctx = ctx.replace('|', '\\|')[:72]
            rows.append('| %s | %s | %s |' % (x['display'], mark, ctx or '—'))

        # 阅读册是否已经把这份 md 渲染进去
        seg = re.search(r'<section class="doc" id="doc%s".*?'
                        r'(?=<section class="doc"|</article>)' % re.escape(aid),
                        page, re.S)
        if page and seg:
            seg = seg.group(0)
            if 'class="highlight"' not in seg:
                per_e.append('index.html 里该篇没有划重点卡：摘要 md 新增/改过但阅读册'
                             '未重建，跑一次 --missing-only 即可')
            else:
                hi = norm_digits(seg.split('<div class="article-body"', 1)[0])
                probes = [x for x in nums if not x['weak']][:4]
                absent = [x['display'] for x in probes if x['num'] not in hi]
                if absent:
                    per_w.append('阅读册摘要卡与 md 可能不同步，卡片里找不到：%s'
                                 % '、'.join(absent))
        elif page:
            per_w.append('index.html 中找不到该篇正文区块')

        if strict:
            per_e.extend('（严格模式）' + w for w in per_w)
            per_w = []
        for e in per_e:
            errors.append('[%s] %s' % (aid, e))
        for w in per_w:
            warns.append('[%s] %s' % (aid, w))
        title = (m.get('title') or '（标题未知）')[:48]
        blocks.append('\n'.join(
            ['## %s｜%s' % (aid, title),
             '- 文件：`summaries/%s.md` ｜ 原文基线：%s' % (aid, base_msg),
             '- 结构/合规：%s' % ('✅ 通过' if not per_e else '✗ %d 项硬错误' % len(per_e)),
             '- 数字溯源：%d 个有效数字（另有 %d 个章节号/年份/月份未纳入）｜'
             '✅ %d 带单位一致 · ◐ %d 仅数字命中 · ❌ %d 未找到' %
             (len(nums), n_weak,
              sum(1 for x in nums if x['level'] == 'exact'),
              sum(1 for x in nums if x['level'] == 'numeric'),
              sum(1 for x in nums if x['level'] == 'none')),
             ''] + (['**硬错误**'] + ['- ✗ ' + e for e in per_e] + ['']
                    if per_e else [])
            + (['**待人工复核**'] + ['- ⚠ ' + w for w in per_w] + ['']
               if per_w else [])
            + (['| 摘要数字 | 原文核对 | 原文片段（供比对） |',
                '|---|---|---|'] + rows + [''] if rows else [])))

    res = {'files': len(files), 'errors': errors, 'warns': warns,
           'numbers': n_num, 'exact': n_exact, 'numeric': n_numeric,
           'missing': n_missing, 'blocks': blocks, 'blocked': blocked}
    if write_report:
        st = now_str()
        rep = ['# 摘要复查报告', '',
               '- 生成时间：%s' % st,
               '- 归档目录：`%s`' % ROOT.replace('\\', '/'),
               '- 摘要文件：%d 篇 ｜ 核对数字：%d 个（✅ %d / ◐ %d / ❌ %d）'
               % (len(files), n_num, n_exact, n_numeric, n_missing),
               '- 硬错误：%d 项 ｜ 待人工复核：%d 项%s'
               % (len(errors), len(warns), ' ｜ 模式：严格（可疑项一律判失败）'
                  if strict else ''), '',
               '> 本报告只做「可自动判定」的一致性检查。**数字在原文里出现 ≠ 摘要理解正确**；'
               '概率、情景、因果、前提与结论仍须人工通读全文判断。', '']
        if not files:
            rep += ['暂无摘要文件。把划重点摘要写成 `summaries/<文章ID>.md` 后，'
                    '跑 `--check-summaries --stamp` 建立基线，再跑 `--missing-only` 重建阅读册。', '']
        rep += blocks
        rep += ['## 待人工复核清单', '']
        rep += (['- ⚠ ' + w for w in warns] if warns else ['- 无（自动检查未发现可疑项）'])
        rep += ['', '## 复查口径与边界', '',
                '- **硬错误**（必改）：标记缺失、ID 不在索引、原文链接对不上、'
                '缺免责声明、原文已变但基线未更新、摘要没渲染进阅读册。',
                '- **待复核**（人判断）：数字未在原文出现、只有裸数字命中（单位/口径可能不同）、'
                '含数字却缺「非一手、需核实原文」、含概率却缺「主观判断」。',
                '- **不检查**：观点对错、逻辑是否成立、行情数据本身是否真实'
                '（原文数据即作者自行整理的非一手来源）、图片内文字。',
                '- 基线用「正文去头部/空白/图片引用后的 md5」；`--rebuild` 若改变了排版，'
                '基线也会变，须按新正文复核后加 `--accept-edits` 再 `--stamp`。'
                '正文未变的篇目无需重新总结，先跑 `--pending-summaries` 看待办清单。',
                '- 改完摘要后必须重跑 `--missing-only` 重建 `index.html`，'
                '否则阅读册里仍是旧卡片。', '']
        rp = os.path.join(ROOT, '摘要复查报告.md')
        with open(rp, 'w', encoding='utf-8') as f:
            f.write('\n'.join(rep))
        res['report'] = rp
    return res


def run_check_summaries(stamp=False, strict=False, accept_edits=False):
    # 纯离线模式不建目录；--root 写错时给可操作的提示，而不是抛 FileNotFoundError
    if (not os.path.isdir(ROOT)
            or not os.path.exists(os.path.join(ROOT, 'metadata.json'))):
        print('复查失败：%s 不是一份已有归档（缺少 metadata.json）。' % ROOT)
        print('请确认 --root 指向归档目录；本模式纯离线，不会创建目录。')
        return False
    res = audit_summaries(write_report=True, stamp=stamp, strict=strict,
                          accept_edits=accept_edits)
    print('摘要复查 —— %d 篇 / %d 个数字（✅ %d 带单位一致 · ◐ %d 仅数字命中 · '
          '❌ %d 未找到）/ 硬错误 %d 项 / 待人工复核 %d 项'
          % (res['files'], res['numbers'], res['exact'], res['numeric'],
             res['missing'], len(res['errors']), len(res['warns'])))
    for e in res['errors']:
        print('  ✗ ' + e)
    if res.get('blocked'):
        print('  ! %d 篇原文已变化，基线未被更新（防止没重读全文就盖章）；'
              '按新正文复核后加 --accept-edits 再跑一次。' % len(res['blocked']))
    for w in res['warns'][:12]:
        print('  ⚠ ' + w)
    if len(res['warns']) > 12:
        print('  … 其余 %d 项见报告' % (len(res['warns']) - 12))
    print('报告：%s' % res.get('report', ''))
    if res['errors']:
        print('复查未通过：先修掉硬错误，再逐条核对报告里的待复核项。')
        return False
    if res['warns']:
        print('⚠ 无硬错误，但有 %d 项待人工复核（未命中数字、口径差异等）；'
              '交付前请打开报告逐条确认。' % len(res['warns']))
        print('  想要「有疑点就不放行」，加 --strict 再跑一次。')
    else:
        print('✓ 无硬错误、无待复核项；仍建议人工通读摘要核对观点与结论。')
    return True


# ---------------------------------------------------------------- 自检 / 改文检测
def size_report():
    """纯离线统计归档体积：按类别汇总，并列出最大的几个文件。

    方便判断「除了原文之外是否存了太大规格的东西」，不删任何文件。
    """
    buckets = {}
    biggest = []
    for dp, _dns, fns in os.walk(ROOT):
        for fn in fns:
            p = os.path.join(dp, fn)
            if not os.path.isfile(p):
                continue
            sz = os.path.getsize(p)
            rel = os.path.relpath(p, ROOT).replace('\\', '/')
            top = rel.split('/')[0] if '/' in rel else '根目录文件'
            b = buckets.setdefault(top, [0, 0])
            b[0] += 1
            b[1] += sz
            biggest.append((sz, rel))
    total = sum(v[1] for v in buckets.values())
    print('磁盘占用 —— 合计 %.2f MB' % (total / 1048576.0))
    for k, (n, s) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
        print('  %-14s %7.2f MB  %3d 个文件' % (k, s / 1048576.0, n))
    for sz, rel in sorted(biggest, reverse=True)[:3]:
        print('  最大：%7.2f MB  %s' % (sz / 1048576.0, rel))


def run_verify(strict=False):
    """纯离线自检，不访问网络。检查归档文件的数量、ID 一致性与内容纯净度。"""
    import glob
    if not os.path.isdir(ROOT) or not os.path.exists(os.path.join(ROOT, 'metadata.json')):
        print('自检失败：%s 不是一份已有归档（缺少 metadata.json）。' % ROOT)
        print('先运行 --missing-only 完成首次归档，或检查 --root 是否指向正确目录。')
        return False
    problems = []
    meta_path = os.path.join(ROOT, 'metadata.json')
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding='utf-8')).get('articles', {})
        except Exception as e:
            problems.append('metadata.json 解析失败：%s' % e)
    mds = glob.glob(os.path.join(ARTICLES, '*.md'))
    raws = glob.glob(os.path.join(ARTICLES, '*.source.html'))
    md_ids = {os.path.basename(p)[:-3] for p in mds}
    raw_ids = {os.path.basename(p).split('.')[0] for p in raws}
    page_path = os.path.join(ROOT, 'index.html')
    page = open(page_path, encoding='utf-8').read() if os.path.exists(page_path) else ''

    if not meta:
        problems.append('metadata.json 缺失或无文章')
    if len(mds) != len(meta) or len(raws) != len(meta):
        problems.append('数量不一致：metadata %d / md %d / 原始HTML %d'
                        % (len(meta), len(mds), len(raws)))
    if md_ids != raw_ids or md_ids != set(meta):
        problems.append('文章 ID 集合不一致：md 独有 %s / 原始HTML 独有 %s'
                        % (sorted(md_ids - raw_ids)[:5], sorted(raw_ids - md_ids)[:5]))

    dirty = [os.path.basename(p) for p in mds + raws
             if '【归档说明】' in open(p, encoding='utf-8').read()]
    if dirty:
        problems.append('%d 个正文文件仍含工具注入文字：%s' % (len(dirty), dirty[:5]))
    # 不再用「正文不足 N 字」判失败：专栏本来就有短文，绝对字数阈值会误伤。
    # 只保留「一个字都没有」这条空内容底线。
    empty = [os.path.basename(p) for p in mds
             if not norm_body(open(p, encoding='utf-8').read().split('\n---\n\n', 1)[-1])]
    if empty:
        problems.append('%d 篇正文为空：%s' % (len(empty), empty[:5]))
    for aid, mm in sorted(meta.items()):
        rel = mm.get('last_full_snapshot')
        if not rel:
            continue
        snap = os.path.join(ROOT, rel)
        if not os.path.exists(snap):
            problems.append('%s 记录的删除前全文快照不存在：%s' % (aid, rel))
        elif not norm_body(open(snap, encoding='utf-8').read().split('\n---\n\n', 1)[-1]):
            problems.append('%s 记录的删除前全文快照为空：%s' % (aid, rel))

    for bad in ['undefined', '<template', '【归档说明】', '归档补图',
                'xqimg-local://']:
        if bad in page:
            problems.append('index.html 含残留内容 %r（%d 处）' % (bad, page.count(bad)))
    n_sum_files = len(glob.glob(os.path.join(ROOT, 'summaries', '*.md')))
    n_hi = page.count('class="highlight"')
    if n_sum_files and n_hi != n_sum_files:
        problems.append('阅读册划重点卡 %d 个，summaries/ 有 %d 篇，需重建阅读册'
                        % (n_hi, n_sum_files))
    if re.search(r'%\(\w+\)s', page):
        problems.append('index.html 有未替换的模板占位符')
    n_item = len(re.findall(r'class="item"', page))
    n_doc = len(re.findall(r'<section class="doc"', page))
    if n_item != len(meta) or n_doc != len(meta):
        problems.append('阅读册目录/正文数量不符：item %d / doc %d / 应为 %d'
                        % (n_item, n_doc, len(meta)))
    refs = set(re.findall(r'images/([A-Za-z0-9_\-]+\.(?:jpg|png))', page))
    missing_imgs = [r for r in refs if not os.path.exists(os.path.join(IMAGES, r))]
    if missing_imgs:
        problems.append('%d 个图片引用指向不存在的文件：%s'
                        % (len(missing_imgs), missing_imgs[:5]))
    img_files = {os.path.basename(p) for p in glob.glob(os.path.join(IMAGES, '*'))
                 if os.path.isfile(p)}
    unused = sorted(img_files - refs)
    # 同一张图被存成多个文件名，通常意味着降级通道误抓了站点广告/占位图。
    # 只比对阅读册真正引用的图：历史孤儿图不该让当前归档自检失败。
    by_hash = {}
    for name in img_files & refs:
        dig = hashlib.md5(open(os.path.join(IMAGES, name), 'rb').read()).hexdigest()
        by_hash.setdefault(dig, []).append(name)
    dups = [v for v in by_hash.values() if len(v) > 1]
    if dups:
        problems.append('%d 组图片内容完全重复，疑似误抓站点通用图：%s'
                        % (len(dups), sorted(dups[0])[:3]))

    for name in ['目录.csv', '观点台账.csv']:
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            problems.append('%s 缺失' % name)
        else:
            n = sum(1 for _ in open(p, encoding='utf-8-sig')) - 1
            if n != len(meta):
                problems.append('%s 行数 %d，应为 %d' % (name, n, len(meta)))

    incomplete = {}
    for aid, mm in meta.items():
        # images 是可选统计字段（无配图文章早期版本不写），缺了不影响功能，不列入
        miss = [k for k in ('fetched_at', 'body_chars', 'topic', 'source')
                if mm.get(k) is None or mm.get(k) == '']
        if miss:
            incomplete[aid] = miss
    if incomplete:
        sample = sorted(incomplete.items())[0]
        print('提示：%d 篇缺少归档专有字段（例：%s 缺 %s），跑一次全量同步可补齐'
              % (len(incomplete), sample[0], '/'.join(sample[1])))

    # 摘要是加工层：硬错误计入自检；数字未命中只提示，不能自动判定观点对错
    try:
        sres = audit_summaries(write_report=False, stamp=False, strict=strict)
    except Exception as e:
        sres = {'files': 0, 'errors': ['摘要复查执行异常：%s' % e], 'warns': []}
    if sres['files'] or sres['errors']:
        print('摘要复查：%d 篇 / 硬错误 %d / 待复核 %d'
              % (sres['files'], len(sres['errors']), len(sres['warns'])))
        problems.extend(sres['errors'])
        for w in sres['warns'][:3]:
            print('  ! 待复核：%s' % w)
        if sres['warns']:
            print('  提示：另有 %d 项摘要待人工复核，跑 --check-summaries 看完整清单'
                  % len(sres['warns']))

    sources = {}
    for m in meta.values():
        s = m.get('source') or '未标注'
        sources[s] = sources.get(s, 0) + 1
    chars = sum(len(open(p, encoding='utf-8').read()) for p in mds)
    print('归档自检 —— %d 篇 / %d 份原始HTML / %d 张图片 / 正文约 %d 字'
          % (len(meta), len(raws), len(img_files), chars))
    print('抓取通道：%s' % ('、'.join('%s %d 篇' % (k, v) for k, v in sorted(sources.items()))))
    if '未标注' in sources:
        print('提示：%d 篇缺少抓取通道/时间戳标注（旧版脚本增量同步可能抹掉），'
              '跑一次全量同步或 --rebuild 可补齐' % sources['未标注'])
    if unused:
        print('提示：%d 张本地图片未被阅读册引用（可能是重复下载的旧版配图）' % len(unused))
    flagged = [(aid, archive_status(m, True)) for aid, m in sorted(meta.items())
               if m.get('remote_missing') or m.get('local_snapshot_reason')]
    if flagged:
        print('提示：%d 篇目前仅保留本地快照（线上未覆盖或取不到可信正文），原文未丢失：'
              % len(flagged))
        for aid, st in flagged[:5]:
            print('  · %s %s' % (aid, st))
    snap_kept = [(aid, m.get('last_full_snapshot', '')) for aid, m in sorted(meta.items())
                 if m.get('last_full_snapshot')]
    if snap_kept:
        print('提示：%d 篇记录了「最后一版完整全文」位置（%d 篇另存 _snapshots/，'
              '--verify 已校验文件存在）'
              % (len(snap_kept), sum(1 for _, p in snap_kept
                                     if p.startswith('_snapshots/'))))
    size_report()
    if problems:
        print('自检失败，共 %d 项：' % len(problems))
        for x in problems:
            print('  ✗ ' + x)
        return False
    print('✓ 全部检查通过：文件齐全、ID 一致、正文纯净、图片引用有效、阅读册无残留')
    return True


def run_check_edits():
    """轻量改文检测：只比对时间线的 edited_at / 标题，不下载正文。"""
    warm()
    print('扫描时间线元数据（不抓正文）…')
    remote = scan_timeline(max_pages=min(30, MAX_PAGES))
    meta_path = os.path.join(ROOT, 'metadata.json')
    local = {}
    if os.path.exists(meta_path):
        try:
            local = json.load(open(meta_path, encoding='utf-8')).get('articles', {})
        except Exception as e:
            print('  ! metadata.json 解析失败，按空归档处理：%s' % e)
    uncovered = sorted(set(local) - set(remote))
    # 更早的文章可能落在时间线扫描范围之外，用单篇接口逐个补齐 edited_at
    for aid in uncovered:
        d = get('https://api.xueqiu.com/statuses/show.json?id=%s' % aid,
                expect='"id"', retries=1)
        if d and d.lstrip().startswith('{'):
            try:
                j = json.loads(d)
                # 被删除的文章可能返回错误对象，不能据此判定「已覆盖」
                if j.get('id'):
                    remote[aid] = {'title': local[aid].get('title', ''),
                                   'edited': ts_str(j.get('edited_at'))}
            except Exception:
                pass
    uncovered = sorted(set(local) - set(remote))
    new_ids, changed, retitled = [], [], []
    for aid, m in remote.items():
        old = local.get(aid)
        if not old:
            new_ids.append((aid, m['title']))
        else:
            if (m.get('edited') or '') != (old.get('edited') or ''):
                changed.append((aid, m['title'], old.get('edited'), m.get('edited')))
            if m.get('title') != old.get('title'):
                retitled.append((aid, old.get('title'), m.get('title')))
    print('本地 %d 篇 / 本次覆盖 %d 篇%s' %
          (len(local), len(remote),
           '（有 %d 篇更早文章未覆盖，需跑全量归档检查）' % len(uncovered)
           if uncovered else '（全部覆盖）'))
    print('新文章 %d 篇、修改时间变化 %d 篇、标题变化 %d 篇' %
          (len(new_ids), len(changed), len(retitled)))
    for aid, t in new_ids[:10]:
        print('  [新增] %s %s' % (aid, t[:36]))
    for aid, t, a, b in changed[:10]:
        print('  [改文] %s %s  %s → %s' % (aid, t[:30], a, b))
    for aid, a, b in retitled[:10]:
        print('  [改标题] %s  %s → %s' % (aid, (a or '')[:24], (b or '')[:24]))
    print('提示：如需抓取改动后的正文，不带 --missing-only 跑一次全量归档（生成旧版快照 + diff）')
    return True


# ---------------------------------------------------------------- 主流程
def main():
    # 非 UTF-8 控制台（GBK 等）也能打印状态符号，不会因为一个 ✓ 崩掉自检
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass
    opts = parse_options()
    # 纯离线/只读模式不建目录，避免在错误的 --root 下留下一堆空文件夹
    no_write = (opts.verify or opts.check_summaries or opts.check_edits
                or opts.pending_summaries or opts.meta_only)
    configure(opts, create_dirs=not no_write)
    if opts.verify:
        sys.exit(0 if run_verify(strict=opts.strict) else 1)
    if opts.pending_summaries:
        sys.exit(0 if run_pending_summaries() else 1)
    if opts.check_summaries:
        sys.exit(0 if run_check_summaries(stamp=opts.stamp,
                                          strict=opts.strict,
                                          accept_edits=opts.accept_edits) else 1)
    if opts.check_edits:
        sys.exit(0 if run_check_edits() else 1)
    rebuild = opts.rebuild
    meta_only = opts.meta_only
    skip_scan = opts.skip_scan
    missing_only = opts.missing_only and not rebuild
    meta_path = os.path.join(ROOT, 'metadata.json')
    old_meta = {}
    if os.path.exists(meta_path):
        try:
            old_meta = json.load(open(meta_path, encoding='utf-8')).get('articles', {})
        except Exception:
            pass
    # --skip-scan 且全部复用（增量 / 只看元数据）时一次网络请求都不发
    if not (skip_scan and (meta_only or missing_only)):
        warm()
    if skip_scan and old_meta:
        meta_all = old_meta
        print('[1/4] 复用已有元数据：%d 篇' % len(meta_all))
    else:
        print('[1/4] 扫描用户时间线，收集全部专栏文章…')
        meta_all = scan_timeline()

    # 本轮时间线未覆盖的旧条目一律按 ID 补回，绝不能按「总数变少」判断：
    # 删 1 篇同时发 1 篇新文时总数不变，按数量判断会静默丢掉被删文章的索引条目。
    # 不猜测原因（删除 / 撤专栏 / 标题清空 / 接口中断都可能），只保留本地快照并标记。
    if old_meta and meta_all is not old_meta:
        absent = sorted(set(old_meta) - set(meta_all))
        for aid in absent:
            keep = dict(old_meta[aid])
            keep['remote_missing'] = True
            keep.setdefault('last_seen', keep.get('fetched_at') or '')
            # 线上彻底删掉时，主 md 就是最后一版完整全文，把位置显式记下来
            keep.setdefault('last_full_snapshot', 'articles/%s.md' % aid)
            # 线上彻底消失时，上一轮的「缩水/抓不到正文」描述已过时，避免状态误导
            keep.pop('shrink_note', None)
            keep.pop('local_snapshot_reason', None)
            meta_all[aid] = keep
        for aid, m in meta_all.items():
            if aid not in absent:
                m['last_seen'] = now_str()
                m.pop('remote_missing', None)
        if absent:
            print('  · 本轮时间线未覆盖 %d 篇（删除/下架/接口中断皆有可能），'
                  '本地快照与索引条目全部保留：%s'
                  % (len(absent), '、'.join(absent[:6])))

    # scan_timeline 每轮重建 dict，不含归档专有字段；继承回来，避免增量同步抹掉
    if old_meta and meta_all is not old_meta:
        kept = merge_archive_fields(meta_all, old_meta)
        if kept:
            print('  · 继承已有归档字段 %d 项（抓取通道、时间戳、阅读量、主题等）' % kept)

    if meta_only:
        print('元数据模式：共 %d 篇。最新 5 篇：' % len(meta_all))
        for m in sorted(meta_all.values(), key=lambda x: x['created_at'] or 0, reverse=True)[:5]:
            print('  %s  %s' % (m['published'][:10], m['title'][:40]))
        return

    print('[2/4] 逐篇抓取全文并本地化配图…')
    article_html, article_imgs, bodies = {}, {}, {}
    changed = []
    drop_ids = []          # 首次归档就抓不到正文的篇目：不入索引，避免集合不一致
    snapshot_stamp = datetime.datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
    total = len(meta_all)
    if rebuild:
        changed.append('【来源统一重建】%d 篇按原始 HTML 全量重抓，清除工具注入文字；'
                       '本轮不生成改文 diff' % total)
    for n, (aid, m) in enumerate(sorted(meta_all.items(),
                                        key=lambda kv: kv[1]['created_at'] or 0, reverse=True), 1):
        path = os.path.join(ARTICLES, aid + '.md')
        raw_path = os.path.join(ARTICLES, aid + '.source.html')
        prev_md = open(path, encoding='utf-8').read() if os.path.exists(path) else ''
        prev_html = (open(raw_path, encoding='utf-8').read()
                     if os.path.exists(raw_path) else '')
        if missing_only and not rebuild and prev_md:
            reuse_local_body(aid, prev_md, raw_path, bodies, article_html, article_imgs)
            if n % 5 == 0:
                print('  %d/%d 复用已有归档' % (n, total))
            continue

        art = fetch_article(m)
        # 抓取失败或疑似占位符：绝不写盘覆盖本地快照，直接把旧正文喂回本轮输出
        reason = ''
        if not art:
            reason = '线上正文不可获取（可能已删除/下架/接口异常）'
        else:
            ok, why = body_quality(art['text_html'], prev_md, opts.accept_shrink,
                                   prev_html=prev_html)
            if not ok:
                reason = why
        if reason:
            m['local_snapshot_reason'] = reason
            m.pop('shrink_note', None)
            if prev_md:
                reuse_local_body(aid, prev_md, raw_path, bodies, article_html, article_imgs)
                # 默认不覆盖：主文件本身就是「线上消失前的最后一版完整全文」
                m.setdefault('last_full_snapshot', 'articles/%s.md' % aid)
                changed.append('【保护】%s %s：%s；沿用本地快照，未覆盖'
                               % (m['title'][:30], aid, reason))
            else:
                # 没有任何本地快照可喂：从索引里也一并去掉，保证
                # metadata / CSV / 阅读册 / articles 四者 ID 集合完全一致；
                # 下次 --missing-only 会重新发现并补抓。
                drop_ids.append(aid)
                changed.append('【跳过】%s %s：%s；首次归档不写入空壳正文，'
                               '本轮不入索引（下次会自动补抓）'
                               % (m['title'][:30], aid, reason))
            print('  (%d/%d) %s 保护：%s' % (n, total, aid, reason))
            continue
        m.pop('local_snapshot_reason', None)

        md, body = build_markdown(m, art)
        prev_body = prev_md.split('\n---\n\n', 1)[-1] if prev_md else ''
        body_changed = bool(prev_md) and norm_body(prev_body) != norm_body(body)
        # 作者主动删改、且已用 --accept-shrink 显式放行：这是唯一会覆盖本地全文的路径
        new_n, old_n = body_stats(art['text_html'], prev_md, prev_html)
        shrunk = bool(opts.accept_shrink and prev_md and old_n and new_n < old_n * 0.9)
        # 覆盖前留档：--rebuild 此前不备份，线上变成占位符时旧正文会被直接抹掉。
        # 缩水时只写一份语义明确的 .before-delete.md，不再把同一份旧正文落盘两次。
        if body_changed:
            if not shrunk:
                save_snapshot(aid, prev_md, snapshot_stamp,
                              'pre-rebuild' if rebuild else '')
            if not rebuild:
                diff = list(difflib.unified_diff(prev_body.splitlines(), body.splitlines(),
                                                 lineterm='', n=1))
                with open(os.path.join(SNAPSHOTS, '%s_%s.diff.txt' % (aid, snapshot_stamp)),
                          'w', encoding='utf-8') as f:
                    f.write('\n'.join(diff[:400]))
                changed.append('【改文】%s %s（旧版与 diff 已存 _snapshots/）' % (m['title'][:30], aid))
        # 把「删除前最后一版全文」的路径写进元数据与阅读册，确保随时找得回
        if shrunk:
            snap_rel = save_snapshot(aid, prev_md, snapshot_stamp, 'before-delete')
            m['last_full_snapshot'] = snap_rel
            m['shrink_note'] = ('线上正文明显缩水（已显式放行）：约 %d 字 → %d 字；'
                                '放行前最后一版全文见 %s' % (old_n, new_n, snap_rel))
            changed.append('【主动删改】%s %s：%s'
                           % (m['title'][:30], aid, m['shrink_note']))
        else:
            m.pop('shrink_note', None)
        with open(raw_path, 'w', encoding='utf-8') as f:
            f.write(art['text_html'])
        article_html[aid] = art['text_html']
        article_imgs[aid] = art['image_map']
        bodies[aid] = body
        if not prev_md and aid not in old_meta:
            changed.append('【新增】%s %s' % (m['title'][:30], aid))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(md)
        m['fetched_at'] = now_str()
        m['body_chars'] = len(body)
        m['images'] = len(art['image_map'])
        m['topic'] = m.get('topic') or topic_of(m['title'])
        m['source'] = art['source']
        if n % 5 == 0 or n == total:
            print('  %d/%d 已归档（图片 %d 张）' %
                  (n, total, sum(len(v) for v in article_imgs.values())))
        time.sleep(0.8 if rebuild else 0.4)

    for aid in drop_ids:
        meta_all.pop(aid, None)
    if drop_ids:
        print('  · %d 篇首次抓取失败，本轮不入索引（下次 --missing-only 会自动补抓）：%s'
              % (len(drop_ids), '、'.join(drop_ids[:6])))

    print('[3/4] 写出元数据、目录、观点台账…')
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({'author': AUTHOR, 'uid': UID, 'column': COLUMN_URL,
                   'last_sync': now_str(), 'articles': meta_all},
                  f, ensure_ascii=False, indent=2)

    print('[4/4] 生成离线阅读册 index.html…')
    n_out = build_outputs(meta_all, bodies, changed, article_html, article_imgs)
    import glob
    print('完成：%d 篇元数据，%d 篇全文，%d 张本地图片，%d 条变更' %
          (len(meta_all), len(bodies), len(glob.glob(os.path.join(IMAGES, '*'))), len(changed)))


if __name__ == '__main__':
    main()
