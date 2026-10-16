# TVBox 自建配置源

**直播 + 点播**一体的 TVBox 配置源工程：多源聚合 → 校验去重 → 定时更新 → 输出 `result.txt` / `source.json`。
纯 Python 标准库实现，**不需要 pip 安装任何依赖**，本机、服务器、GitHub Actions 都能直接跑。

---

## 一、目录结构

```
tvbox-source/
├── config.ini                     # 主配置模板（直播 + 点播所有核心参数）
├── tvbox_builder.py               # 抓取 / 校验 / 去重 / 排序 / 输出 主程序
├── config/
│   ├── subscribe.txt              # 直播订阅源清单（每行一个，可写 UA）
│   ├── blacklist.txt              # 域名 / 关键字黑名单
│   ├── category.txt               # 自定义频道菜单（CCTV / 卫视 / 地方台 …）
│   ├── demo.txt                   # 频道模板：决定频道顺序与必出频道
│   ├── local.txt                  # 本地补充直播源（手工钉住的稳定地址）
│   ├── sites_source.txt           # ★点播源聚合来源（公开 TVBox 配置）
│   └── sites.txt                  # ★手工点播源（优先保留）
├── output/                        # 运行后自动生成，已被 Actions 提交回仓库
│   ├── result.txt                 # ★直播主产物：TVBox 标准 txt 直播源
│   ├── result.m3u                 # ★直播 m3u 版本（带台标）
│   ├── source.json                # ★综合配置：lives 直播 + sites 点播，直接当「配置地址」用
│   ├── sites.json                 # ★纯点播配置：只含 sites/spider/parses
│   ├── report.json                # 本次运行统计（live / vod 两段）
│   └── update.log                 # 运行日志
└── .github/workflows/update.yml   # GitHub Actions 每日定时任务
```

---

## 二、核心参数一览（`config.ini`）

### 直播（lives）

| 参数 | 段 | 默认值 | 说明 |
|---|---|---|---|
| `subscribe_urls` | `[source]` | 5 个已验证源 | 订阅源列表，逗号分隔，支持 m3u / txt |
| `subscribe_file` | `[source]` | `config/subscribe.txt` | 订阅源清单文件，支持 `地址 UA="xxx"` |
| `domain_blacklist` | `[source]` | 见文件 | **域名黑名单**，子串匹配，命中即丢弃 |
| `url_keywords_blacklist` | `[source]` | 广告/测试/… | URL 或频道名命中关键字即丢弃 |
| `urls_limit` | `[source]` | `5` | **每频道保留的源数量**（排序后截断） |
| `ipv_type` | `[source]` | `ipv4` | **IPv4/IPv6 过滤**：`ipv4` / `ipv6` / `all` |
| `ipv_type_prefer` | `[source]` | `auto` | 结果里哪类地址排前面 |
| `min_resolution` | `[source]` | `0` | 最小分辨率：`0` / `720` / `1080` / `2160` |
| `max_response_time` | `[source]` | `3000` | 响应超过该毫秒数即丢弃，`0` 不限制 |
| `probe_timeout` / `probe_workers` | `[source]` | `4` / `48` | 单源超时（秒）/ 并发线程数 |
| `max_candidates` | `[source]` | `3000` | 单轮最多探测多少条，`0` 不限 |

### 点播（sites）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `open_sites` | `True` | 是否开启点播源聚合 |
| `sites_source_urls` | 6 个已验证配置 | 点播源聚合来源（公开 TVBox 配置 JSON） |
| `sites_source_file` | `config/sites_source.txt` | 聚合来源清单文件 |
| `sites_file` | `config/sites.txt` | 手工点播源，优先排在结果最前 |
| `sites_limit` | `150` | 点播源总数上限，`0` 不限 |
| `probe_sites` | `True` | 对 type=1 的 CMS 接口做 `ac=list` 真校验 |
| `sites_probe_timeout` | `6` | CMS 接口校验超时（秒） |
| `sites_keywords_blacklist` | 广告/加群/… | 点播源名称黑名单 |
| `spider` | 空 | 爬虫包地址，留空则从来源中自动选出现次数最多的 |
| `parses_limit` | `40` | 解析接口保留数量上限 |

### 定时与输出

| 参数 | 段 | 默认值 | 说明 |
|---|---|---|---|
| `final_file` / `final_m3u_file` | `[output]` | `output/result.txt` / `.m3u` | 直播结果路径 |
| `source_json` / `sites_json` | `[output]` | `output/source.json` / `sites.json` | 综合配置 / 纯点播配置 |
| `publish_base_url` | `[output]` | 空 | `source.json` 里 lives 指向的在线地址前缀 |
| `daily_time` / `cron` | `[schedule]` | `06:00` / `0 6 * * *` | 仅备注用，真正生效的定时在 Actions 或系统 crontab 里 |

> 直播参数名 `subscribe_urls` / `domain_blacklist` / `urls_limit` / `ipv_type` 与 Guovin/TV（原 iptv-api）、LazyImmortal/TVLiveSource 等主流项目同名同义，以后想迁到那些项目可以直接复制。

---

## 三、直播源：推荐的初始订阅源

全部为 2026-10-16 实测可达（HTTP 200 且有内容），已预置在 `config/subscribe.txt`：

| # | 地址 | 类型 | 特点 |
|---|---|---|---|
| 1 | `https://raw.githubusercontent.com/Guovin/TV/gd/output/result.txt` | txt | 央视+卫视+地方台最全，自带每日更新，**首选** |
| 2 | `https://raw.githubusercontent.com/bestK/iptv/main/iptv.txt` | txt | 每小时检测，540+ 频道，含港澳台 |
| 3 | `https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u` | m3u | 老牌精选，画质偏高 |
| 4 | `https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u` | m3u | 合集型，量大 |
| 5 | `https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u` | m3u | 纯 IPv4 |
| 6 | `https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.txt` | txt | 纯 IPv4 |
| 7 | `https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u` | m3u | iptv-org 官方中国列表 |
| 8 | `https://m3u.ibert.me/txt/o_cn.txt` | txt | 需带 UA，备用不易失联 |
| 9 | `https://m3u.ibert.me/txt/fmml_ipv6.txt` | txt | 纯 IPv6 精选 |
| 10 | `https://peterhchina.github.io/iptv/CCTV-V4.m3u` | m3u | 央视 IPv4 专用 |

本轮实测**未连通**、已注释掉的候选（可自行 `curl` 验证后再启用）：
`live.zbds.top`、`kimwang1978/collect-tv-txt`、`hujingguang/ChinaIPTV`。
另附 `https://gh-proxy.com/` 作为 raw.githubusercontent 不通时的 CDN 回退，脚本已内置自动重试。

---

## 四、点播源（非直播）怎么工作

TVBox 配置里的 `sites` 就是点播，每条 = 一个影视资源站。三种类型，校验方式完全不同：

| type | 含义 | 能否 HTTP 校验 | 本工具的处理 |
|---|---|---|---|
| `1` | CMS JSON 采集接口（苹果CMS 风格 `api.php/provide/vod/`） | **能** | 请求 `?ac=list`，返回带 `class`/`list` 的合法 JSON 才算活，失败直接丢弃 |
| `3` | 内置 `csp_XXX` 或 js（drpy）源 | 不能，依赖 spider 爬虫包 | 跨源去重后原样保留 |
| `0` | 爬虫 / 嗅探源 | 不能，依赖 spider | 跨源去重后原样保留 |

工具会从 6 个公开 TVBox 配置里抓取并合并四样东西：

| 抽取内容 | 说明 |
|---|---|
| `sites` | 点播资源站，按类型/名称/接口去重 |
| `spider` | 爬虫包地址，按「出现次数最多」自动投票选出，并解析成**绝对地址**（保留 `;md5;hash` 后缀，TVBox 靠它校验包完整性） |
| `parses` | 播放解析接口，去重后合并（自动过滤 `Demo`、`./xxx` 这类无效项） |
| `flags` | 播放线路标记，去重后合并 |

### 去重策略（关键细节）

- **type=1**：按「主机 + 路径」去重，忽略 `http/https` 与结尾斜杠差异 —— 这样 `http://caiji.dyttzyapi.com/api.php/provide/vod` 和 `https://caiji.dyttzyapi.com/api.php/provide/vod/` 会被合并成一条。
- **type=3 js 源**：按 `ext` 的文件名去重，所以同一个 js 源出现在不同镜像仓库时只保留一份（如 `qist/js/播客.js` 与 `gao/js/播客.js` 视为同一个）。
- **相对路径**：配置里的 `./lib/drpy2.min.js`、`./js/xxx.js` 会按来源配置的地址解析成绝对 URL，避免合并后路径失效。

### 实测数据（本机跑的一轮）

6 个点播来源全部抓取成功 → 原始 **848 条** → 去重丢弃 **326 条** → 校验 14 个 CMS 接口（**8 个有效 / 6 个失效**）→ 按 `sites_limit` 截断为 **150 条**（type 分布：`1:13 / 3:136 / 0:1`），`parses` 33 条，`spider` 自动选定。

### 已预置的点播来源（均实测 HTTP 200 且可解析）

| 来源 | 站点数 | 特点 |
|---|---|---|
| `qist/tvbox/master/jsm.json` | 154 | 主力配置，CMS 采集接口最全 |
| `qist/tvbox/master/dianshi.json` | 155 | 电视向，直播+点播混合 |
| `qist/tvbox/master/367.json` | 106 | 大而全合集 |
| `qist/tvbox/master/XYQ.json` | 49 | 轻量，JSONC 注释写法 |
| `gaotianliuyun/gao/master/0821.json` | 86 | 经典配置，站点质量高 |
| `gaotianliuyun/gao/master/js.json` | 298 | js 源为主，站点数最多 |

**关于 JSONC 与加密配置**：工具已兼容 BOM、`//` 与 `/* */` 注释、尾部多余逗号（公开配置大量使用）。
遇到**加密/混淆配置**（内容是十六进制串，如 `yoursmile66/TVBox/main/XC.json`）会自动识别、告警并跳过 —— 这类配置任何工具都无法直接聚合，加进去也不会产生站点。

### 手工点播源 `config/sites.txt`

支持两种写法，内容会**优先排在 `sites` 数组最前面**且不做删减：

```
# 写法一：名称,type,api[,ext]
电影天堂采集,1,https://caiji.dyttzyapi.com/api.php/provide/vod/

# 写法二：一行完整 JSON，把 TVBox 配置里的 site 对象原样贴进来
{"key":"mysite","name":"我的接口","type":1,"api":"https://my-domain.com/api.php/provide/vod/","searchable":1}
```

已预置 5 个 2026-10-16 实测可用（`?ac=list` 返回标准 JSON）的公开 CMS 接口。

---

## 五、本地先跑一遍

```bash
cd tvbox-source

# 完整跑（直播测速 + 点播聚合校验，约 4~8 分钟）
python tvbox_builder.py

# 调试：直播部分不测速，只看抓取解析
python tvbox_builder.py --skip-probe

# 小机器/网络慢：限制直播候选数与并发
python tvbox_builder.py --max-candidates 800 --workers 16
```

跑完看 `output/report.json`，`live` 段是直播统计（原始/过滤/有效/频道数/命中率），`vod` 段是点播统计（sites 数、type 分布、spider、parses、CMS 有效率）。

---

## 六、部署方案 A：GitHub Actions（推荐，零成本）

1. **建仓库**：新建一个公开仓库（公开仓库 Actions 免费且不限分钟数）。
2. **上传文件**：把 `tvbox-source/` 下所有内容推到仓库根目录，`.github/workflows/update.yml` 要一起传上去。`.gitignore` 特意**没有**忽略 `output/`，否则 Action 提交不回去。
3. **开权限**：仓库 `Settings → Actions → General → Workflow permissions` 选 **Read and write permissions**。
4. **改配置**：编辑 `config.ini`，把
   ```ini
   publish_base_url = https://raw.githubusercontent.com/你的用户名/仓库名/main
   ```
   填成你自己的仓库地址（分支名按实际改）。
5. **手动试跑**：`Actions → 每日更新 TVBox 直播源 → Run workflow`，跑完确认 `output/result.txt`、`output/source.json` 都已更新。
6. **定时生效**：`update.yml` 里已配好 `cron: "0 22 * * *"`，即 **UTC 22:00 = 北京时间每日 06:00**。
   **Actions 的 cron 一律按 UTC 计算**，对照表：

   | 想要的北京时间 | 应填的 cron |
   |---|---|
   | 每日 06:00 | `0 22 * * *` |
   | 每日 08:00 | `0 0 * * *` |
   | 每日 18:00 | `0 10 * * *` |
   | 每天 06:00 和 18:00 | `0 22,10 * * *` |
   | 每 6 小时一次 | `0 */6 * * *` |

7. **TVBox 接入**：在 TVBox / 影视仓里把「配置地址」填成
   `https://raw.githubusercontent.com/你的用户名/仓库名/main/output/source.json`
   （这个文件里 `lives` 和 `sites` 都有，直播点播一次配好）。
   只想用点播就填 `.../main/output/sites.json`。
   国内直连 raw 不稳时可换
   `https://gh-proxy.com/https://raw.githubusercontent.com/你的用户名/仓库名/main/output/source.json`

> 注意：仓库要**公开**，TVBox 才能免鉴权拉到地址。公开仓库不要放任何私人信息。

---

## 七、部署方案 B：自己的服务器

### B1. 直接挂 crontab（最简单）

```bash
sudo mkdir -p /opt/tvbox-source && sudo chown $USER /opt/tvbox-source
cd /opt/tvbox-source          # 把整个 tvbox-source 目录内容拷进去
python3 tvbox_builder.py      # 先手动跑一次确认没问题
crontab -e
```

```cron
# 每天 06:00 更新 TVBox 直播源 + 点播源
0 6 * * * cd /opt/tvbox-source && /usr/bin/python3 tvbox_builder.py >> /opt/tvbox-source/output/cron.log 2>&1
```

服务器是 UTC 时区就改成 `0 22 * * *`（= 北京 06:00）。

### B2. systemd timer（更规范，可查状态）

`/etc/systemd/system/tvbox-update.service`
```ini
[Unit]
Description=TVBox live+vod source daily update
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=your_user
WorkingDirectory=/opt/tvbox-source
ExecStart=/usr/bin/python3 /opt/tvbox-source/tvbox_builder.py
```

`/etc/systemd/system/tvbox-update.timer`
```ini
[Unit]
Description=Run TVBox update daily at 06:00 Asia/Shanghai

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
Unit=tvbox-update.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tvbox-update.timer
systemctl list-timers tvbox-update.timer     # 查看下次运行时间
journalctl -u tvbox-update.service -n 50     # 查看上次运行日志
```

### B3. Nginx 把结果暴露成在线地址

`config.ini` 里 `publish_base_url = https://tv.your-domain.com`，然后：

```nginx
server {
    listen 443 ssl;
    server_name tv.your-domain.com;

    location /output/ {
        alias /opt/tvbox-source/output/;
        add_header Cache-Control "no-cache";
        default_type text/plain;
        charset utf-8;
    }
}
```

TVBox 配置地址即 `https://tv.your-domain.com/output/source.json`。用 Docker 的话，把 `output/` 挂给任意静态服务器容器即可。

---

## 八、结果格式说明

### `output/result.txt`（直播）

```
更新时间,2026-10-16 06:00:03

CCTV,#genre#
CCTV-1 综合,http://a.com/cctv1.m3u8#http://b.com/cctv1.m3u8
CCTV-2 财经,http://c.com/cctv2.m3u8

卫视,#genre#
湖南卫视,http://d.com/hunan.m3u8
```

- 同频道多源用 `#` 分隔，播放器会自动切换。
- 分类名固定以 `,#genre#` 结尾，这是 TVBox 分组语法。
- 直播排序：**分辨率高的优先 → 响应快的优先 → 同分时 IPv4 优先**，再按 `urls_limit` 截断。
- 打开 `open_url_info = True` 会在地址后追加 `$来源|分辨率|协议`；PotPlayer 等解析不了会导致不能播，故默认关闭。

### `output/source.json`（综合配置）

```jsonc
{
  "spider": "https://.../jar/spider.jar;md5;bca2498f...",   // 爬虫包，csp/js 源依赖它
  "sites":  [ /* 150 个点播站：手工源 > 校验过的 CMS > js/csp 源 */ ],
  "lives":  [ {"name":"电视直播(TXT)","type":0,"url":"<publish_base_url>/output/result.txt","playerType":1} ],
  "parses": [ /* 解析接口 */ ],
  "flags":  [ /* 播放线路标记 */ ]
}
```

`output/sites.json` 是同样的内容但 `lives` 为空，供只想用点播的场景。
`source.json` 的排序保证：手工源永远在最前，其次是校验通过且响应快的 CMS 接口。

---

## 九、常见问题

| 现象 | 原因与处理 |
|---|---|
| 直播 `result.txt` 是空的 | 多半没抓到订阅源。先 `--skip-probe` 看抓取阶段有没有「抓取失败」。 |
| 点播 `sites` 是空的 | 看日志里「疑似加密/混淆配置」告警；换一个纯 JSON 来源，或在 `config/sites.txt` 手工加。 |
| 点播站点能列出但点开播不了 | 大概率是 `spider` 没生效。确认 `source.json` 里 `spider` 是绝对地址且带 `;md5;` 后缀；或自己在 `config.ini` 的 `spider` 里写死一个。 |
| raw.githubusercontent 抓不到 | 脚本已自动走 `cdn_url` 里的 GH 代理回退；也可在 `[filter] http_proxy` 里配代理。 |
| 直播有效源太少 | 调大 `probe_timeout`（4→6）、放宽 `max_response_time`、把 `min_resolution` 设回 0。 |
| 直播校验通过但播不了 | 开启 `open_url_info` + `min_resolution=720` 能显著减少这类情况。 |
| 想只要 IPv6 干净源 | `ipv_type = ipv6`，并确保网络支持 IPv6，否则会全军覆没。 |
| 点播站太多导致 TVBox 卡 | 调小 `sites_limit`，建议 100~150。 |
| Actions 跑完没提交 | 没开 `Read and write permissions`；或误把 `output/` 加进了 `.gitignore`。 |
| 频道名没合并到一起 | 在 `config/category.txt` 补关键字，或在 `tvbox_builder.py` 的 `CCTV_MAP` 里补别名。 |
| 并发大时进程被杀 | 本机把 `probe_workers` 降到 16；Actions 上 48 没问题。 |

---

## 十、内容合规提醒

本工具只做「公开链接的抓取、校验与格式转换」。聚合来的直播源与点播接口，其版权与可用性均由原始提供方决定，
请仅用于个人学习与自用测试，不要用于商业传播或二次分发；如需长期稳定观看，请使用正规授权渠道。
