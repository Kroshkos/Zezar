FROM hub.docker.alibaba-inc.com/aone-base-global/python:312

# 设置工作目录
WORKDIR /app

# 复制项目代码
COPY . .

# 安装依赖（放在复制代码前，提高缓存利用率）
RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 暴露端口
EXPOSE 8000

# 启动命令（调整路径，直接使用工作目录）
CMD ["python", "openhands/action_execution_server.py", "8000", "--working-dir", "/workspace"]