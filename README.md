# IncandescenceArchiver_repair
GPT6 VibeCoding

————————————————————————————

修复头像

python ..\..\avatar_repair.py scan
扫描本地缺失资源

python ..\..\avatar_repair.py download
扫描并下载本地缺失资源（不依赖scan）

————————————————————————————

修复推文媒体

python ..\..\imgvid_repair.py scan
扫描本地缺失资源

python ..\..\imgvid_repair.py download
扫描并下载本地缺失资源（不依赖scan）

python ..\..\imgvid_repair.py scan --type images/videos

python ..\..\imgvid_repair.py download --type images/videos

无type默认all

————————————————————————————

状态清洗

python ..\..\status_cleaner.py scan
扫描不正确状态，生成日志

python ..\..\status_cleaner.py apply
备份主状态索引和现有 TXT，修正不正确状态，重新生成正确状态 TXT

————————————————————————————

修复HTML图片引用

python ..\..\html_media_repair.py scan
检测JSON及本地文件存在，但HTML缺失引用的图片

python ..\..\html_media_repair.py apply
备份HTML，为HTML重新插入缺失的本地图片引用

————————————————————————————

反向查询引用源

python ..\..\find_media_refs.py mediakey/avatarid/tweetid/link
