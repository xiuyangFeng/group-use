import os
import tempfile

# 测试时处理记录写到临时目录，不碰本机真正的记录
os.environ["DEID_HOME"] = tempfile.mkdtemp(prefix="deid_home_")
