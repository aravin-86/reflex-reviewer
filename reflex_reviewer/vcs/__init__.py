from ..config import get_vcs_config
from .bitbucket_vcs import BitbucketVCSClient


def get_vcs_client(vcs_type=None, config_overrides=None):
    resolved_vcs_config = get_vcs_config(config_overrides)
    resolved_vcs_type = (
        str(
            vcs_type
            if vcs_type is not None
            else resolved_vcs_config.get("type", "bitbucket")
        )
        .strip()
        .lower()
    )

    if resolved_vcs_type == "bitbucket":
        return BitbucketVCSClient(resolved_vcs_config)
    if resolved_vcs_type == "oci_devops_scm":
        raise NotImplementedError(
            "VCS client for 'oci_devops_scm' is not implemented yet."
        )
    if resolved_vcs_type == "github":
        raise NotImplementedError("VCS client for 'github' is not implemented yet.")
    raise ValueError(
        f"Unsupported VCS type '{resolved_vcs_type}'. Supported values: bitbucket, oci_devops_scm, github"
    )
