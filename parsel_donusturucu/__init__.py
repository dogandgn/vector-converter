# Developed by dogandgn with Codex Astra (GPT-6) and QGIS MCP.


def classFactory(iface):
    from .plugin import ParselPlugin

    return ParselPlugin(iface)
