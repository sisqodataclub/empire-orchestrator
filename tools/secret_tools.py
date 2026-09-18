###
# tools/secret_tools.py
from crewai.tools import tool

# Global reference to the current SecretsManager (set by ActiveTask)
_current_secrets_manager = None

def set_secrets_manager(manager):
    global _current_secrets_manager
    _current_secrets_manager = manager

@tool("Set Secret")
def set_secret(key: str, value: str):
    """Stores a secret value for later use. The value is never displayed or logged."""
    if _current_secrets_manager:
        _current_secrets_manager.set(key, value)
        return f"Secret '{key}' saved."
    return "Error: Secrets manager not initialized."

@tool("Get Secret")
def get_secret(key: str):
    """Retrieves a secret value. Only use inside a script; never display the value."""
    if _current_secrets_manager:
        val = _current_secrets_manager.get(key)
        if val:
            return val
        return f"Secret '{key}' not found."
    return "Error: Secrets manager not initialized."

@tool("List Secret Keys")
def list_secret_keys():
    """Lists available secret keys (without values)."""
    if _current_secrets_manager:
        keys = _current_secrets_manager.list_keys()
        return ", ".join(keys) if keys else "No secrets stored."
    return "Error: Secrets manager not initialized."

@tool("Delete Secret")
def delete_secret(key: str):
    """Deletes a secret."""
    if _current_secrets_manager:
        _current_secrets_manager.delete(key)
        return f"Secret '{key}' deleted."
    return "Error: Secrets manager not initialized."
