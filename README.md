# IncandescenceArchiver_repair
GPT6 VibeCoding

python apply_convert_filename_fix.py  
一次性补丁 修正archive.py中convert使用的safe_filename错误的问题  

python apply_render_media_priority_fix.py  
一次性补丁 修正archive.py中render-html、clean-html、build-index优先选择convert生成的错误filename文件的问题

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

————————————————————————————

补充视频预览图

python ..\..\preview_image_repair.py scan  
扫描本地缺失资源

python ..\..\preview_image_repair.py download  
扫描并下载本地缺失资源（不依赖scan）

python ..\..\preview_image_repair.py convert "G:\工具\webarchived_tweet_downloader\username_archive"  
从 webarchived_tweet_downloader 导入

————————————————————————————

修复convert已经生成的错误filename  

python ../../fix_convert_media_names.py  
扫描本地错误filename文件

python ../../fix_convert_media_names.py --apply  
回收哈希重复的错误文件、修正非重复错误文件的文件名、修复HTML和index.json的旧引用并生成备份

python ../../fix_convert_media_names.py --delete-trash  
检查回收站并删除回收文件

python ../../fix_convert_media_names.py --delete-backups  
删除已备份的旧HTML和index
