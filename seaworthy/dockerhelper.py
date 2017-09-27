import logging

import docker

from .utils import resource_name

log = logging.getLogger(__name__)


def fetch_images(client, images):
    """
    Fetch images if they aren't already present.
    """
    return [fetch_image(client, image) for image in images]


def fetch_image(client, name):
    """
    Fetch an image if it isn't already present.
    """
    try:
        image = client.images.get(name)
    except docker.errors.ImageNotFound:
        log.info("Pulling tag '{}'...".format(name))
        image = client.images.pull(name)

    log.debug("Found image '{}' for tag '{}'".format(image.id, name))
    return image


class DockerHelper(object):
    _network = None
    _container_ids = None
    _client = None

    def setup(self):
        self._client = docker.client.from_env()
        self._container_ids = set()

        self._setup_network()

    def _setup_network(self):
        # Docker allows the creation of multiple networks with the same name
        # (unlike containers). This seems to cause problems sometimes with
        # container networking for some reason (?).
        name = resource_name('default')
        if self._client.networks.list(names=[name]):
            raise RuntimeError(
                "A network with the name '{}' already exists".format(name))

        self._network = self._client.networks.create(name, driver='bridge')

    def teardown(self):
        if self._client is None:
            return

        # Remove all containers
        for container_id in self._container_ids.copy():
            # Check if the container exists before trying to remove it
            try:
                container = self._client.containers.get(container_id)
            except docker.errors.NotFound:
                continue

            log.warning("Container '{}' still existed during teardown".format(
                container.name))

            if container.status == 'running':
                self.stop_container(container)
            self.remove_container(container)
        self._container_ids = None

        # Remove the network
        if self._network is not None:
            self._network.remove()
            self._network = None

        # We need to close the underlying APIClient explicitly to avoid
        # ResourceWarnings from unclosed HTTP connections.
        self._client.api.close()
        self._client = None

    def create_container(self, name, image, **kwargs):
        container_name = resource_name(name)
        log.info("Creating container '{}'...".format(container_name))
        container = self._client.containers.create(
            image, name=container_name, detach=True, network=self._network.id,
            **kwargs)

        # FIXME: Hack to make sure the container has the right network aliases.
        # Only the low-level Docker client API allows us to specify endpoint
        # aliases at container creation time:
        # https://docker-py.readthedocs.io/en/stable/api.html#docker.api.container.ContainerApiMixin.create_container
        # If we don't specify a network when the container is created then the
        # default bridge network is attached which we don't want, so we
        # reattach our custom network as that allows specifying aliases.
        self._network.disconnect(container)
        self._network.connect(container, aliases=[name])

        # Keep a reference to created containers to make sure they are cleaned
        # up
        self._container_ids.add(container.id)

        return container

    def container_status(self, container):
        container.reload()
        log.debug("Container '{}' has status '{}'".format(
            container.name, container.status))
        return container.status

    def start_container(self, container):
        log.info("Starting container '{}'...".format(container.name))
        container.start()
        # If the container is short-lived, it may have finished and exited
        # before we check its status.
        assert self.container_status(container) in ['running', 'exited']

    def stop_container(self, container, timeout=5):
        log.info("Stopping container '{}'...".format(container.name))
        container.stop(timeout=timeout)
        assert self.container_status(container) != 'running'

    def remove_container(self, container, force=True):
        log.info("Removing container '{}'...".format(container.name))
        container.remove(force=force)

        self._container_ids.remove(container.id)

    def stop_and_remove_container(
            self, container, stop_timeout=5, remove_force=True):
        self.stop_container(container, timeout=stop_timeout)
        self.remove_container(container, force=remove_force)

    def pull_image_if_not_found(self, image):
        return fetch_image(self._client, image)
