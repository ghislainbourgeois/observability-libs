# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""# KubernetesComputeResourcesPatch Library.

This library is designed to enable developers to more simply patch the Kubernetes compute resource
limits and requests created by Juju during the deployment of a sidecar charm.

When initialised, this library binds a handler to the parent charm's `config-changed` event, which
applies the patch to the cluster. This should ensure that the resource limits are correct
throughout the charm's life. Additional optional user-provided events for re-applying the patch are
supported but discouraged.

The constructor takes a reference to the parent charm, a 'limits' and a 'requests' dictionaries
that together define the resource requirements. For information regarding the `lightkube`
`ResourceRequirements` model, please visit the `lightkube`
[docs](https://gtsystem.github.io/lightkube-models/1.23/models/core_v1/#resourcerequirements).


## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`. **Note
that you also need to add `lightkube` and `lightkube-models` to your charm's `requirements.txt`.**

```shell
cd some-charm
charmcraft fetch-lib charms.observability_libs.v0.kubernetes_compute_resources_patch
cat << EOF >> requirements.txt
lightkube
lightkube-models
EOF
```

Then, to initialise the library:

```python
# ...
from charms.observability_libs.v0.kubernetes_compute_resources_patch import (
    KubernetesComputeResourcesPatch
)

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...
    self.resources_patch = KubernetesComputeResourcesPatch(
        self,
        "container-name",
        limits={"cpu": "1", "mem": "2Gi"},
        requests={"cpu": "1", "mem": "2Gi"},
        refresh_event=self.on.config_changed
    )
    # ...
```

Additionally, you may wish to use mocks in your charm's unit testing to ensure that the library
does not try to make any API calls, or open any files during testing that are unlikely to be
present, and could break your tests. The easiest way to do this is during your test `setUp`:

```python
# ...

@patch("charm.KubernetesComputeResourcesPatch", lambda *a, **kw: None)
def setUp(self):
    self.harness = Harness(SomeCharm)
    # ...
```
"""

import logging
from types import MethodType
from typing import List, Optional, TypedDict, Union

from lightkube import ApiError, Client
from lightkube.core import exceptions
from lightkube.models.apps_v1 import StatefulSetSpec
from lightkube.models.core_v1 import (
    Container,
    PodSpec,
    PodTemplateSpec,
    ResourceRequirements,
)
from lightkube.resources.apps_v1 import StatefulSet
from lightkube.resources.core_v1 import Pod
from lightkube.types import PatchType
from ops.charm import CharmBase
from ops.framework import BoundEvent, Object

logger = logging.getLogger(__name__)

# The unique Charmhub library identifier, never change it
LIBID = "2a6066f701444e8db44ba2f6af28da90"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1


class ResourceSpecDict(TypedDict, total=False):
    """A dict representing a K8s resource limit.

    See:
    - https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/
    - https://gtsystem.github.io/lightkube-models/1.23/models/core_v1/#resourcerequirements
    """

    cpu: str
    memory: str


class KubernetesComputeResourcesPatch(Object):
    """A utility for patching the Kubernetes compute resources set up by Juju."""

    def __init__(
        self,
        charm: CharmBase,
        container_name: str,
        *,
        limits: Optional[ResourceSpecDict],
        requests: Optional[ResourceSpecDict],
        refresh_event: Optional[Union[BoundEvent, List[BoundEvent]]] = None,
    ):
        """Constructor for KubernetesComputeResourcesPatch.

        References:
            - https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/

        Args:
            charm: the charm that is instantiating the library.
            container_name: the container for which to apply the resource limits.
            limits: a dictionary for `limits` resources.
            requests: a dictionary for `requests` resources.
            refresh_event: an optional bound event or list of bound events which
                will be observed to re-apply the patch.
        """
        super().__init__(charm, "kubernetes-compute-resource-patch")
        self.charm = charm
        self.container_name = container_name
        self.resource_reqs = ResourceRequirements(
            limits=limits,  # type: ignore[arg-type]
            requests=requests,  # type: ignore[arg-type]
        )
        self.patched_delta = self._patched_delta(
            namespace=self._namespace,
            app_name=self._app,
            container_name=self.container_name,
            resource_reqs=self.resource_reqs,
        )

        # Make mypy type checking happy that self._patch is a method
        assert isinstance(self._patch, MethodType)
        # Ensure this patch is applied during the 'config-changed' event, which is emitted every
        # startup and every upgrade. The config-changed event is a good time to apply this kind of
        # patch because it is always emitted after storage-attached, leadership and peer-created,
        # all of which only fire after install. Patching the statefulset prematurely could result
        # in those events firing without a workload.
        self.framework.observe(charm.on.config_changed, self._patch)

        if not refresh_event:
            refresh_event = []
        elif not isinstance(refresh_event, list):
            refresh_event = [refresh_event]
        for ev in refresh_event:
            self.framework.observe(ev, self._patch)

    @classmethod
    def _patched_delta(
        cls,
        namespace: str,
        app_name: str,
        container_name: str,
        resource_reqs: ResourceRequirements,
    ) -> StatefulSet:
        client = Client()
        statefulset = client.get(StatefulSet, name=app_name, namespace=namespace)

        return StatefulSet(
            spec=StatefulSetSpec(
                selector=statefulset.spec.selector,  # type: ignore[attr-defined]
                serviceName=statefulset.spec.serviceName,  # type: ignore[attr-defined]
                template=PodTemplateSpec(
                    spec=PodSpec(
                        containers=[Container(name=container_name, resources=resource_reqs)]
                    )
                ),
            )
        )

    def _patch(self, _) -> None:
        """Patch the Kubernetes resources created by Juju to limit cpu or mem."""
        try:
            client = Client()
        except exceptions.ConfigError as e:
            logger.warning("Error creating k8s client: %s", e)
            return
        if self._is_patched(client):
            return

        try:

            client.patch(
                StatefulSet,
                self._app,
                self.patched_delta,
                namespace=self._namespace,
                patch_type=PatchType.APPLY,
                field_manager=self.__class__.__name__,
            )
        except ApiError as e:
            if e.status.code == 403:
                logger.error("Kubernetes resources patch failed: `juju trust` this application.")
            else:
                logger.error("Kubernetes resources patch failed: %s", str(e))
        else:
            logger.info(
                "Kubernetes resources for app '%s', container '%s' patched successfully: %s",
                self._app,
                self.container_name,
                self.resource_reqs,
            )

    def is_patched(self) -> bool:
        """Reports if the resource patch has been applied.

        Returns:
            bool: A boolean indicating if the service patch has been applied.
        """
        client = Client()
        return self._is_patched(client)

    def is_ready(self):
        """Reports if the resource patch has been applied and is in effect.

        Returns:
            bool: A boolean indicating if the service patch has been applied and is in effect.
        """
        client = Client()
        pod = client.get(Pod, name=self._pod, namespace=self._namespace)
        podspec = self._get_container(self.container_name, pod.spec.containers)  # type: ignore[attr-defined]
        return self._is_patched(client) and self.resource_reqs == podspec.resources

    @classmethod
    def _get_container(cls, container_name: str, containers: List[Container]) -> Container:
        """Find our container from the container list, assuming list is unique by name.

        Typically, *.spec.containers[0] is the charm container, and [1] is the (only) workload.

        Raises:
            StopIteration, if the user-provided container name does not exist in the list.

        Returns:
            An instance of :class:`Container` whose name matches the given name.
        """
        return next(iter(filter(lambda ctr: ctr.name == container_name, containers)))

    def _is_patched(self, client: Client) -> bool:
        """Reports if the resource patch has been applied to the StatefulSet.

        Returns:
            bool: A boolean indicating if the service patch has been applied.
        """
        statefulset = client.get(StatefulSet, name=self._app, namespace=self._namespace)
        podspec_tpl = self._get_container(
            self.container_name,
            statefulset.spec.template.spec.containers,  # type: ignore[attr-defined]
        )
        return podspec_tpl.resources == self.resource_reqs

    @property
    def _app(self) -> str:
        """Name of the current Juju application.

        Returns:
            str: A string containing the name of the current Juju application.
        """
        return self.charm.app.name

    @property
    def _pod(self) -> str:
        """Name of the unit's pod.

        Returns:
            str: A string containing the name of the current unit's pod.
        """
        return "-".join(self.charm.unit.name.rsplit("/", 1))

    @property
    def _namespace(self) -> str:
        """The Kubernetes namespace we're running in.

        If a charm is deployed into the controller model (which certainly could happen as we move
        to representing the controller as a charm) then self.charm.model.name !== k8s namespace.
        Instead, the model name is controller in Juju and controller-<controller-name> for the
        namespace in K8s.

        Returns:
            str: A string containing the name of the current Kubernetes namespace.
        """
        with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
            return f.read().strip()
