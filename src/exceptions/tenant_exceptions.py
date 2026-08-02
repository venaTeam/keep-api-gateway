class TenantNameConflict(Exception):
    """Raised when a tenant with the requested name already exists."""


class TenantNotFound(Exception):
    """Raised when the referenced tenant does not exist."""


class OperatorGroupTaken(Exception):
    """Raised when the group already backs another operator (global uniqueness)."""
