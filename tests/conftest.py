"""测试时把 AstrBot 的根目录挪到临时目录。

AstrBot 导入时会按 ASTRBOT_ROOT（默认当前工作目录）创建 data/ 目录树。
在插件目录里跑测试就会顺手写出一份 data/，看着像漏进来的脏文件 ——
这里提前把它指到临时目录，插件目录保持干净。
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault("ASTRBOT_ROOT", tempfile.mkdtemp(prefix="bili-whale-test-"))
