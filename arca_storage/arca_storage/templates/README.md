# Templates

This directory contains Jinja2 templates for configuration file generation.

## ganesha.conf.j2

NFS-Ganesha configuration template.

### Template Variables

- `template_version`: Template version (e.g., "1.0.0")
- `config_version`: Configuration version timestamp (e.g., "20251220123456")
- `exports`: List of export dictionaries with the following keys:
  - `export_id`: Unique export ID (integer)
  - `path`: Export path (e.g., "/exports/tenant_a/vol1")
  - `pseudo`: Pseudo path (e.g., "/exports/tenant_a/vol1")
  - `access`: Access type ("RW" or "RO")
  - `squash`: Squash mode ("Root_Squash" or "No_Root_Squash")
  - `sec`: Security types (list, e.g., ["sys"])
  - `client`: Client CIDR (e.g., "10.0.0.0/24")

### Usage Example

```python
from jinja2 import Template
from pathlib import Path

template_path = Path("templates/ganesha.conf.j2")
template = Template(template_path.read_text())

config = template.render(
    template_version="1.0.0",
    config_version="20251220123456",
    exports=[
        {
            "export_id": 101,
            "path": "/exports/tenant_a/vol1",
            "pseudo": "/exports/tenant_a/vol1",
            "access": "RW",
            "squash": "Root_Squash",
            "sec": ["sys"],
            "client": "10.0.0.0/24"
        }
    ]
)

# Write to file
Path("/etc/ganesha/ganesha.tenant_a.conf").write_text(config)
```

## Version Management

- Template files are version-controlled in Git
- Generated configuration files include both `template_version` and `config_version` in their headers
- `config_version` is derived from the rendered content (stable for identical inputs) so repeated renders are idempotent
