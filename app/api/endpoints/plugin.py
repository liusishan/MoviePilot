import mimetypes
import shutil
from typing import Annotated, Any, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from starlette import status
from starlette.responses import FileResponse

from app import schemas
from app.command import Command
from app.core.config import settings
from app.core.plugin import PluginManager
from app.core.security import verify_apikey, verify_token
from app.db.systemconfig_oper import SystemConfigOper
from app.db.user_oper import get_current_active_superuser
from app.factory import app
from app.helper.plugin import PluginHelper
from app.log import logger
from app.scheduler import Scheduler
from app.schemas.types import SystemConfigKey

PROTECTED_ROUTES = {"/api/v1/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
PLUGIN_PREFIX = f"{settings.API_V1_STR}/plugin"

router = APIRouter()


def register_plugin_api(plugin_id: Optional[str] = None):
    """
    动态注册插件 API
    :param plugin_id: 插件 ID，如果为 None，则注册所有插件
    """
    _update_plugin_api_routes(plugin_id, action="add")


def remove_plugin_api(plugin_id: str):
    """
    动态移除单个插件的 API
    :param plugin_id: 插件 ID
    """
    _update_plugin_api_routes(plugin_id, action="remove")


def _update_plugin_api_routes(plugin_id: Optional[str], action: str):
    """
    插件 API 路由注册和移除
    :param plugin_id: 插件 ID，如果 action 为 "add" 且 plugin_id 为 None，则处理所有插件
                      如果 action 为 "remove"，plugin_id 必须是有效的插件 ID
    :param action: "add" 或 "remove"，决定是添加还是移除路由
    """
    if action not in {"add", "remove"}:
        raise ValueError("Action must be 'add' or 'remove'")

    is_modified = False
    existing_paths = {route.path: route for route in app.routes}

    plugin_ids = [plugin_id] if plugin_id else PluginManager().get_running_plugin_ids()
    for plugin_id in plugin_ids:
        routes_removed = _remove_routes(plugin_id)
        if routes_removed:
            is_modified = True

        if action != "add":
            continue
        # 获取插件的 API 路由信息
        plugin_apis = PluginManager().get_plugin_apis(plugin_id)
        for api in plugin_apis:
            api_path = f"{PLUGIN_PREFIX}{api.get('path', '')}"
            try:
                api["path"] = api_path
                allow_anonymous = api.pop("allow_anonymous", False)
                auth_mode = api.pop("auth", "apikey")
                dependencies = api.setdefault("dependencies", [])
                if not allow_anonymous:
                    if auth_mode == "bear" and Depends(verify_token) not in dependencies:
                        dependencies.append(Depends(verify_token))
                    elif Depends(verify_apikey) not in dependencies:
                        dependencies.append(Depends(verify_apikey))
                app.add_api_route(**api, tags=["plugin"])
                is_modified = True
                logger.debug(f"Added plugin route: {api_path}")
            except Exception as e:
                logger.error(f"Error adding plugin route {api_path}: {str(e)}")

    if is_modified:
        _clean_protected_routes(existing_paths)
        app.openapi_schema = None
        app.setup()


def _remove_routes(plugin_id: str) -> bool:
    """
    移除与单个插件相关的路由
    :param plugin_id: 插件 ID
    :return: 是否有路由被移除
    """
    if not plugin_id:
        return False
    prefix = f"{PLUGIN_PREFIX}/{plugin_id}/"
    routes_to_remove = [route for route in app.routes if route.path.startswith(prefix)]
    removed = False
    for route in routes_to_remove:
        try:
            app.routes.remove(route)
            removed = True
            logger.debug(f"Removed plugin route: {route.path}")
        except Exception as e:
            logger.error(f"Error removing plugin route {route.path}: {str(e)}")
    return removed


def _clean_protected_routes(existing_paths: dict):
    """
    清理受保护的路由，防止在插件操作中被删除或重复添加
    :param existing_paths: 当前应用的路由路径映射
    """
    for protected_route in PROTECTED_ROUTES:
        try:
            existing_route = existing_paths.get(protected_route)
            if existing_route:
                app.routes.remove(existing_route)
        except Exception as e:
            logger.error(f"Error removing protected route {protected_route}: {str(e)}")


def register_plugin(plugin_id: str):
    """
    注册一个插件相关的服务
    """
    # 注册插件服务
    Scheduler().update_plugin_job(plugin_id)
    # 注册菜单命令
    Command().init_commands(plugin_id)
    # 注册插件API
    register_plugin_api(plugin_id)


@router.get("/", summary="所有插件", response_model=List[schemas.Plugin])
def all_plugins(_: schemas.TokenPayload = Depends(get_current_active_superuser),
                state: Optional[str] = "all", force: bool = False) -> List[schemas.Plugin]:
    """
    查询所有插件清单，包括本地插件和在线插件，插件状态：installed, market, all
    """
    # 本地插件
    local_plugins = PluginManager().get_local_plugins()
    # 已安装插件
    installed_plugins = [plugin for plugin in local_plugins if plugin.installed]
    if state == "installed":
        return installed_plugins
        
    # 未安装的本地插件
    not_installed_plugins = [plugin for plugin in local_plugins if not plugin.installed]
    # 在线插件
    online_plugins = PluginManager().get_online_plugins(force)
    if not online_plugins:
        # 没有获取在线插件
        if state == "market":
            # 返回未安装的本地插件
            return not_installed_plugins
        return local_plugins

    # 插件市场插件清单
    market_plugins = []
    # 已安装插件IDS
    _installed_ids = [plugin.id for plugin in installed_plugins]
    # 未安装的线上插件或者有更新的插件
    for plugin in online_plugins:
        if plugin.id not in _installed_ids:
            market_plugins.append(plugin)
        elif plugin.has_update:
            market_plugins.append(plugin)
    # 未安装的本地插件，且不在线上插件中
    _plugin_ids = [plugin.id for plugin in market_plugins]
    for plugin in not_installed_plugins:
        if plugin.id not in _plugin_ids:
            market_plugins.append(plugin)
    # 返回插件清单
    if state == "market":
        # 返回未安装的插件
        return market_plugins
        
    # 返回所有插件
    return installed_plugins + market_plugins


@router.get("/installed", summary="已安装插件", response_model=List[str])
def installed(_: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    查询用户已安装插件清单
    """
    return SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []


@router.get("/statistic", summary="插件安装统计", response_model=dict)
def statistic(_: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    插件安装统计
    """
    return PluginHelper().get_statistic()


@router.get("/reload/{plugin_id}", summary="重新加载插件", response_model=schemas.Response)
def reload_plugin(plugin_id: str, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    重新加载插件
    """
    # 重新加载插件
    PluginManager().reload_plugin(plugin_id)
    # 注册插件服务
    register_plugin(plugin_id)
    return schemas.Response(success=True)


@router.get("/install/{plugin_id}", summary="安装插件", response_model=schemas.Response)
def install(plugin_id: str,
            repo_url: Optional[str] = "",
            force: Optional[bool] = False,
            _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    安装插件
    """
    # 已安装插件
    install_plugins = SystemConfigOper().get(SystemConfigKey.UserInstalledPlugins) or []
    # 首先检查插件是否已经存在，并且是否强制安装，否则只进行安装统计
    if not force and plugin_id in PluginManager().get_plugin_ids():
        PluginHelper().install_reg(pid=plugin_id)
    else:
        # 插件不存在或需要强制安装，下载安装并注册插件
        if repo_url:
            state, msg = PluginHelper().install(pid=plugin_id, repo_url=repo_url)
            # 安装失败则直接响应
            if not state:
                return schemas.Response(success=False, message=msg)
        else:
            # repo_url 为空时，也直接响应
            return schemas.Response(success=False, message="没有传入仓库地址，无法正确安装插件，请检查配置")
    # 安装插件
    if plugin_id not in install_plugins:
        install_plugins.append(plugin_id)
        # 保存设置
        SystemConfigOper().set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 重新加载插件
    reload_plugin(plugin_id)
    return schemas.Response(success=True)


@router.get("/remotes", summary="获取插件联邦组件列表", response_model=List[dict])
def remotes(token: str) -> Any:
    """
    获取插件联邦组件列表
    """
    if token != "moviepilot":
        raise HTTPException(status_code=403, detail="Forbidden")
    return PluginManager().get_plugin_remotes()


@router.get("/form/{plugin_id}", summary="获取插件表单页面")
def plugin_form(plugin_id: str,
                _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件配置表单或Vue组件URL
    """
    plugin_instance = PluginManager().running_plugins.get(plugin_id)
    if not plugin_instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"插件 {plugin_id} 不存在或未加载")

    # 渲染模式
    render_mode, _ = plugin_instance.get_render_mode()
    try:
        conf, model = plugin_instance.get_form()
        return {
            "render_mode": render_mode,
            "conf": conf,
            "model": PluginManager().get_plugin_config(plugin_id) or model
        }
    except Exception as e:
        logger.error(f"插件 {plugin_id} 调用方法 get_form 出错: {str(e)}")
    return {}


@router.get("/page/{plugin_id}", summary="获取插件数据页面")
def plugin_page(plugin_id: str, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件数据页面
    """
    plugin_instance = PluginManager().running_plugins.get(plugin_id)
    if not plugin_instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"插件 {plugin_id} 不存在或未加载")

    # 渲染模式
    render_mode, _ = plugin_instance.get_render_mode()
    try:
        page = plugin_instance.get_page()
        return {
            "render_mode": render_mode,
            "page": page or []
        }
    except Exception as e:
        logger.error(f"插件 {plugin_id} 调用方法 get_page 出错: {str(e)}")
    return {}


@router.get("/dashboard/meta", summary="获取所有插件仪表板元信息")
def plugin_dashboard_meta(_: schemas.TokenPayload = Depends(verify_token)) -> List[dict]:
    """
    获取所有插件仪表板元信息
    """
    return PluginManager().get_plugin_dashboard_meta()


@router.get("/dashboard/{plugin_id}/{key}", summary="获取插件仪表板配置")
def plugin_dashboard_by_key(plugin_id: str, key: str, user_agent: Annotated[str | None, Header()] = None,
                            _: schemas.TokenPayload = Depends(verify_token)) -> Optional[schemas.PluginDashboard]:
    """
    根据插件ID获取插件仪表板
    """
    return PluginManager().get_plugin_dashboard(plugin_id, key, user_agent)


@router.get("/dashboard/{plugin_id}", summary="获取插件仪表板配置")
def plugin_dashboard(plugin_id: str, user_agent: Annotated[str | None, Header()] = None,
                     _: schemas.TokenPayload = Depends(verify_token)) -> schemas.PluginDashboard:
    """
    根据插件ID获取插件仪表板
    """
    return plugin_dashboard_by_key(plugin_id, "", user_agent)


@router.get("/reset/{plugin_id}", summary="重置插件配置及数据", response_model=schemas.Response)
def reset_plugin(plugin_id: str,
                 _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    根据插件ID重置插件配置及数据
    """
    plugin_manager = PluginManager()
    # 删除配置
    plugin_manager.delete_plugin_config(plugin_id)
    # 删除插件所有数据
    plugin_manager.delete_plugin_data(plugin_id)
    # 重新加载插件
    reload_plugin(plugin_id)
    return schemas.Response(success=True)


@router.get("/file/{plugin_id}/{filepath:path}", summary="获取插件静态文件")
def plugin_static_file(plugin_id: str, filepath: str):
    """
    获取插件静态文件
    """
    # 基础安全检查
    if ".." in filepath or ".." in plugin_id:
        logger.warning(f"Static File API: Path traversal attempt detected: {plugin_id}/{filepath}")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    plugin_base_dir = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
    plugin_file_path = plugin_base_dir / filepath
    if not plugin_file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{plugin_file_path} 不存在")
    if not plugin_file_path.is_file():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"{plugin_file_path} 不是文件")

    # 判断 MIME 类型
    response_type, _ = mimetypes.guess_type(str(plugin_file_path))
    suffix = plugin_file_path.suffix.lower()
    # 强制修正 .mjs 和 .js 的 MIME 类型
    if suffix in ['.js', '.mjs']:
        response_type = 'application/javascript'
    elif suffix == '.css' and not response_type:  # 如果 guess_type 没猜对 css，也修正
        response_type = 'text/css'
    elif not response_type:  # 对于其他猜不出的类型
        response_type = 'application/octet-stream'

    try:
        return FileResponse(plugin_file_path, media_type=response_type)
    except Exception as e:
        logger.error(f"Error creating/sending FileResponse for {plugin_file_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get("/folders", summary="获取插件文件夹配置", response_model=dict)
def get_plugin_folders(_: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    获取插件文件夹分组配置
    """
    try:
        result = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
        return result
    except Exception as e:
        logger.error(f"[文件夹API] 获取文件夹配置失败: {str(e)}")
        return {}


@router.post("/folders", summary="保存插件文件夹配置", response_model=schemas.Response)
def save_plugin_folders(folders: dict, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    保存插件文件夹分组配置
    """
    try:
        SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(success=True)
    except Exception as e:
        logger.error(f"[文件夹API] 保存文件夹配置失败: {str(e)}")
        return schemas.Response(success=False, message=str(e))


@router.post("/folders/{folder_name}", summary="创建插件文件夹", response_model=schemas.Response)
def create_plugin_folder(folder_name: str, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    创建新的插件文件夹
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    if folder_name not in folders:
        folders[folder_name] = []
        SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(success=True, message=f"文件夹 '{folder_name}' 创建成功")
    else:
        return schemas.Response(success=False, message=f"文件夹 '{folder_name}' 已存在")


@router.delete("/folders/{folder_name}", summary="删除插件文件夹", response_model=schemas.Response)
def delete_plugin_folder(folder_name: str, _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    删除插件文件夹
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    if folder_name in folders:
        del folders[folder_name]
        SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
        return schemas.Response(success=True, message=f"文件夹 '{folder_name}' 删除成功")
    else:
        return schemas.Response(success=False, message=f"文件夹 '{folder_name}' 不存在")


@router.put("/folders/{folder_name}/plugins", summary="更新文件夹中的插件", response_model=schemas.Response)
def update_folder_plugins(folder_name: str, plugin_ids: List[str], _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    更新指定文件夹中的插件列表
    """
    folders = SystemConfigOper().get(SystemConfigKey.PluginFolders) or {}
    folders[folder_name] = plugin_ids
    SystemConfigOper().set(SystemConfigKey.PluginFolders, folders)
    return schemas.Response(success=True, message=f"文件夹 '{folder_name}' 中的插件已更新")


@router.get("/{plugin_id}", summary="获取插件配置")
def plugin_config(plugin_id: str,
                  _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> dict:
    """
    根据插件ID获取插件配置信息
    """
    return PluginManager().get_plugin_config(plugin_id)


@router.put("/{plugin_id}", summary="更新插件配置", response_model=schemas.Response)
def set_plugin_config(plugin_id: str, conf: dict,
                      _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    更新插件配置
    """
    plugin_manager = PluginManager()
    # 保存配置
    plugin_manager.save_plugin_config(plugin_id, conf)
    # 重新生效插件
    plugin_manager.init_plugin(plugin_id, conf)
    # 注册插件服务
    register_plugin(plugin_id)
    return schemas.Response(success=True)


@router.delete("/{plugin_id}", summary="卸载插件", response_model=schemas.Response)
def uninstall_plugin(plugin_id: str,
                     _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    卸载插件
    """
    config_oper = SystemConfigOper()
    # 删除已安装信息
    install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
    for plugin in install_plugins:
        if plugin == plugin_id:
            install_plugins.remove(plugin)
            break
    config_oper.set(SystemConfigKey.UserInstalledPlugins, install_plugins)
    # 移除插件API
    remove_plugin_api(plugin_id)
    # 移除插件服务
    Scheduler().remove_plugin_job(plugin_id)
    # 判断是否为分身
    plugin_manager = PluginManager()
    plugin_class = plugin_manager.plugins.get(plugin_id)
    if getattr(plugin_class, "is_clone", False):
        # 如果是分身插件，则删除分身数据和配置
        plugin_manager.delete_plugin_config(plugin_id)
        plugin_manager.delete_plugin_data(plugin_id)
        # 删除分身文件
        plugin_base_dir = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        if plugin_base_dir.exists():
            try:
                shutil.rmtree(plugin_base_dir)
                plugin_manager.plugins.pop(plugin_id, None)
            except Exception as e:
                logger.error(f"删除插件分身目录 {plugin_base_dir} 失败: {str(e)}")
    # 从插件文件夹中移除该插件
    _remove_plugin_from_folders(plugin_id)
    # 移除插件
    plugin_manager.remove_plugin(plugin_id)
    return schemas.Response(success=True)


@router.post("/clone/{plugin_id}", summary="创建插件分身", response_model=schemas.Response)
def clone_plugin(plugin_id: str,
                 clone_data: dict,
                 _: schemas.TokenPayload = Depends(get_current_active_superuser)) -> Any:
    """
    创建插件分身
    """
    try:
        success, message = PluginManager().clone_plugin(
            plugin_id=plugin_id,
            suffix=clone_data.get("suffix", ""),
            name=clone_data.get("name", ""),
            description=clone_data.get("description", ""),
            version=clone_data.get("version", ""),
            icon=clone_data.get("icon", "")
        )
        
        if success:
            # 注册插件服务
            reload_plugin(message)
            # 将分身插件添加到原插件所在的文件夹中
            _add_clone_to_plugin_folder(plugin_id, message)
            return schemas.Response(success=True, message="插件分身创建成功")
        else:
            return schemas.Response(success=False, message=message)
    except Exception as e:
        logger.error(f"创建插件分身失败：{str(e)}")
        return schemas.Response(success=False, message=f"创建插件分身失败：{str(e)}")


def _add_clone_to_plugin_folder(original_plugin_id: str, clone_plugin_id: str):
    """
    将分身插件添加到原插件所在的文件夹中
    :param original_plugin_id: 原插件ID
    :param clone_plugin_id: 分身插件ID
    """
    try:
        config_oper = SystemConfigOper()
        # 获取插件文件夹配置
        folders = config_oper.get(SystemConfigKey.PluginFolders) or {}
        
        # 查找原插件所在的文件夹
        target_folder = None
        for folder_name, folder_data in folders.items():
            if isinstance(folder_data, dict) and 'plugins' in folder_data:
                # 新格式：{"plugins": [...], "order": ..., "icon": ...}
                if original_plugin_id in folder_data['plugins']:
                    target_folder = folder_name
                    break
            elif isinstance(folder_data, list):
                # 旧格式：直接是插件列表
                if original_plugin_id in folder_data:
                    target_folder = folder_name
                    break
        
        # 如果找到了原插件所在的文件夹，则将分身插件也添加到该文件夹中
        if target_folder:
            folder_data = folders[target_folder]
            if isinstance(folder_data, dict) and 'plugins' in folder_data:
                # 新格式
                if clone_plugin_id not in folder_data['plugins']:
                    folder_data['plugins'].append(clone_plugin_id)
                    logger.info(f"已将分身插件 {clone_plugin_id} 添加到文件夹 '{target_folder}' 中")
            elif isinstance(folder_data, list):
                # 旧格式
                if clone_plugin_id not in folder_data:
                    folder_data.append(clone_plugin_id)
                    logger.info(f"已将分身插件 {clone_plugin_id} 添加到文件夹 '{target_folder}' 中")
            
            # 保存更新后的文件夹配置
            config_oper.set(SystemConfigKey.PluginFolders, folders)
        else:
            logger.info(f"原插件 {original_plugin_id} 不在任何文件夹中，分身插件 {clone_plugin_id} 将保持独立")
            
    except Exception as e:
        logger.error(f"处理插件文件夹时出错：{str(e)}")
        # 文件夹处理失败不影响插件分身创建的整体流程


def _remove_plugin_from_folders(plugin_id: str):
    """
    从所有文件夹中移除指定的插件
    :param plugin_id: 要移除的插件ID
    """
    try:
        config_oper = SystemConfigOper()
        # 获取插件文件夹配置
        folders = config_oper.get(SystemConfigKey.PluginFolders) or {}
        
        # 标记是否有修改
        modified = False
        
        # 遍历所有文件夹，移除指定插件
        for folder_name, folder_data in folders.items():
            if isinstance(folder_data, dict) and 'plugins' in folder_data:
                # 新格式：{"plugins": [...], "order": ..., "icon": ...}
                if plugin_id in folder_data['plugins']:
                    folder_data['plugins'].remove(plugin_id)
                    logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                    modified = True
            elif isinstance(folder_data, list):
                # 旧格式：直接是插件列表
                if plugin_id in folder_data:
                    folder_data.remove(plugin_id)
                    logger.info(f"已从文件夹 '{folder_name}' 中移除插件 {plugin_id}")
                    modified = True
        
        # 如果有修改，保存更新后的文件夹配置
        if modified:
            config_oper.set(SystemConfigKey.PluginFolders, folders)
        else:
            logger.debug(f"插件 {plugin_id} 不在任何文件夹中，无需移除")
            
    except Exception as e:
        logger.error(f"从文件夹中移除插件时出错：{str(e)}")
        # 文件夹处理失败不影响插件卸载的整体流程
