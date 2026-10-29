"""Build enable requests from the same plugin facts presented for review."""

from pulsara_agent.plugins.contracts import (
    ExternalProcessAcceptance,
    InspectLocalPluginsRequest,
    PluginInspectionDisposition,
    SetLocalPluginEnabledRequest,
)
from pulsara_agent.plugins.mcp_connection import connection_review
from pulsara_agent.settings import LOCAL_SETTINGS_FILE_NAME, LocalSettingsStore


def enable_reviewed_plugin(service, installed, deadline):
    identity = installed.identity
    request = InspectLocalPluginsRequest(deadline)
    inspection = service.inspect_local_plugins(request, identity=identity)
    try:
        assert inspection.disposition is PluginInspectionDisposition.COMPLETE
        (instance,) = inspection.instances
        assert instance.identity == identity
        assert instance.package_install_id == installed.package_install_id
        settings = LocalSettingsStore(service._store().home / LOCAL_SETTINGS_FILE_NAME)
        review = connection_review(
            instance.mcp_connection_overlays,
            settings.read().mcp_secret,
            servers=instance.summary.mcp.mcp_servers,
            identity=identity,
        )
    finally:
        inspection.close()
    return service.set_local_plugin_enabled(
        SetLocalPluginEnabledRequest(
            identity.scope,
            identity.plugin_id,
            True,
            installed.package_install_id,
            deadline,
            external_process_acceptance=ExternalProcessAcceptance.ACCEPTED,
            connection_review=review,
        )
    )
