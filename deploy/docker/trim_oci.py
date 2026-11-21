"""Trim the OCI Python SDK to the modules we use. It ships models for every OCI service (~470 MB);
the SDK imports service modules lazily, so removing unused ones is safe."""
import os
import shutil
import sys

KEEP = {"object_storage", "identity", "identity_data_plane", "secrets", "vault", "key_management",
        "dns", "work_requests"}  # imported by oci.pagination and oci.waiter

root = os.path.join(sys.argv[1], "oci")
if not os.path.isdir(root):
    sys.exit(0)
with open(os.path.join(root, "__init__.py")) as f:
    src = f.read()
listed = src.split('"developer_tool_configuration",', 1)[1].split("]", 1)[0]
services = {s.strip().strip('"') for s in listed.replace("\n", "").split(",")} - {""}
removed = 0
for name in sorted(services - KEEP):
    path = os.path.join(root, name)
    if os.path.isdir(path):
        shutil.rmtree(path)
        removed += 1
print(f"oci sdk: removed {removed} unused service modules")

# fail the image build (not the pods) if the trim broke anything we use
import oci
import oci.config  # noqa: F401
from oci.auth.signers import InstancePrincipalsSecurityTokenSigner  # noqa: F401
from oci.object_storage import ObjectStorageClient  # noqa: F401

print("oci sdk: imports used by the services verified")
