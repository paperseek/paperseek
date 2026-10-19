# 自己运行 PaperSeek

社区版包含检索引擎、Web 界面、本地历史、引用图谱和实验性综述。可以选择 Python 安装，也可以使用持久化 Docker 服务。

只想先试试？[本地快速上手](../get-started/local.md)会带你一步步完成第一次检索。

## 安装 Python 包

需要 **Python 3.12 或更新版本**。发行包自带已构建的网页，只有开发前端时才需要 Node.js。

```sh
python -m pip install "paperseek==1.0.0b3"
paperseek serve
```

打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)，在界面中配置模型与数据源——具体步骤见[本地快速上手](../get-started/local.md)。

也可以从源码安装：

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
python -m pip install ./backend
paperseek serve
```

如果源码检出中没有已构建的页面，先按[开发指南](../developers/contributing.md)构建前端，再制作发行包。

## Docker Compose

使用仓库中的 `compose.yaml`：

```sh
git clone https://github.com/paperseek/paperseek.git
cd paperseek
cp .env.example .env
PAPERSEEK_VERSION=1.0.0-beta3 docker compose up -d
```

PowerShell 中将 `cp` 换成 `Copy-Item`，然后执行 `$env:PAPERSEEK_VERSION="1.0.0-beta3"; docker compose up -d`。启动前编辑 `.env`，填写需要的默认配置。

Compose 默认只发布 **127.0.0.1:8765**；`PAPERSEEK_PORT` 修改宿主机端口，容器内部固定为 8765。服务端口为 **8765**，数据存入 `paperseek-data` 命名卷。以上命令显式固定为 `1.0.0-beta3`，随附的 Compose 与 `.env.example` 也使用此默认版本。预发布不会更新 `latest`；升级时请明确调整 `PAPERSEEK_VERSION`。

```sh
docker compose logs -f paperseek
docker compose stop
docker compose start
```

`docker compose down` 保留命名卷，添加 `--volumes` 会删除卷及其中的数据。

## 数据与凭据

本地数据库默认为 `~/.paperseek/paperseek.db`。可通过 `--database` 或 `PAPERSEEK_DATABASE` 更换路径。

数据目录中的数据库、产物和本地凭据加密密钥需要一并保留，不要放进源码仓库或公开共享目录。任务提交的运行凭据与研究历史分开处理。

直接运行 CLI 和本地服务时读取的是进程环境，**不会自动加载旁边的 `.env`**；Docker Compose 会显式加载该文件。

## 分开运行 API 与 worker

默认 `serve` 会同时启动 worker。需要独立管理进程时，让两个进程使用同一数据库：

```sh
paperseek serve --database /srv/paperseek/paperseek.db --no-worker
paperseek worker --database /srv/paperseek/paperseek.db
```

不要遗漏 `--no-worker`，以免意外多启一个 worker。

## 网络访问 {#network-access}

默认服务只监听本机。社区版面向可信本地环境；CLI 使用 `--host 0.0.0.0`，或 Compose 设置 `PAPERSEEK_HOST=0.0.0.0`，会向其他设备开放访问。共享前请配置合适的认证与网络访问控制。更换监听地址不会把社区版变成在线版的多用户系统。

## 备份

先停止 API 与 worker，再复制整个数据目录或 Docker 卷。恢复时先用独立目录验证历史、结果与产物，再替换正在使用的数据。

接下来查看[配置参考](../reference/configuration.md)或[升级指南](upgrade.md)。
