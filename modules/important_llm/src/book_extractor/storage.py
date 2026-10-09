"""使用标准库替换检查点，仅补充Windows共享锁冲突的有限重试。

不实现文件系统或原子写入协议。调用者负责写完并关闭临时文件；本模块
只移动同一份已完成文件，重试不会触发模型调用。
"""

import os
import time
from pathlib import Path
from sys import platform

REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)
WINDOWS_SHARING_ERRORS = {5, 32, 33}


def replace_checkpoint(temporary: str | Path, destination: str | Path) -> None:
    """将完整临时文件替换到目标路径，容忍短暂的Windows读句柄占用。

    参数：
        temporary：已经关闭写入句柄的临时文件，应与目标位于同一文件系统。
        destination：检查点目标路径；成功后原目标被替换。

    返回：
        无返回值。成功后临时文件已移动至目标路径。

    异常：
        仅Windows访问/共享冲突最多重试5次，累计等待1.55秒；重试耗尽或
        遇到其他错误时抛出原异常。失败不删除临时文件，便于恢复和排查。
    """
    for attempt in range(len(REPLACE_RETRY_DELAYS) + 1):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as error:
            # 错误5也可能来自永久权限不足，因此不能只凭错误码无限重试。
            if platform != "win32":
                raise
            if getattr(error, "winerror", None) not in WINDOWS_SHARING_ERRORS:
                raise
            if attempt == len(REPLACE_RETRY_DELAYS):
                raise
            time.sleep(REPLACE_RETRY_DELAYS[attempt])
