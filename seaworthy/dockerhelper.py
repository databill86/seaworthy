import logging

import docker

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


def _parse_volume_short_form(short_form):
    parts = short_form.split(':', 1)
    bind = parts[0]
    mode = parts[1] if len(parts) == 2 else 'rw'
    return {'bind': bind, 'mode': mode}


class HelperBase:
    def __init__(self, collection, namespace):
        self.collection = collection
        self.namespace = namespace

        self._resource_type = self.collection.model.__name__.lower()
        self._ids = set()

    def _resource_name(self, name):
        return '{}_{}'.format(self.namespace, name)

    def _get_id_and_model(self, id_or_model):
        """
        Get both the model and ID of an object that could be an ID or a model.
        :param id_or_model:
            The object that could be an ID string or a model object.
        :param model_collection:
            The collection to which the model belongs.
        """
        if isinstance(id_or_model, self.collection.model):
            model = id_or_model
        elif isinstance(id_or_model, str):
            # Assume we have an ID string
            model = self.collection.get(id_or_model)
        else:
            raise TypeError('Unexpected type {}, expected {} or {}'.format(
                type(id_or_model), str, self.collection.model))

        return model.id, model

    def create(self, name, *args, **kwargs):
        resource_name = self._resource_name(name)
        log.info(
            "Creating {} '{}'...".format(self._resource_type, resource_name))
        resource = self.collection.create(*args, name=resource_name, **kwargs)
        self._ids.add(resource.id)
        return resource

    def remove(self, resource, **kwargs):
        log.info(
            "Removing {} '{}'...".format(self._resource_type, resource.name))
        resource.remove(**kwargs)
        self._ids.remove(resource.id)

    def _teardown(self):
        for resource_id in self._ids.copy():
            # Check if the resource exists before trying to remove it
            try:
                resource = self.collection.get(resource_id)
            except docker.errors.NotFound:
                continue

            log.warning("{} '{}' still existed during teardown".format(
                self._resource_type.title(), resource.name))

            self._teardown_remove(resource)

    def _teardown_remove(self, resource):
        # Override in subclass for different removal behaviour on teardown
        self.remove(resource)


class ContainerHelper(HelperBase):
    def __init__(self, client, namespace, image_helper, network_helper,
                 volume_helper):
        super().__init__(client.containers, namespace)
        self._image_helper = image_helper
        self._network_helper = network_helper
        self._volume_helper = volume_helper

    def create(self, name, image, fetch_image=False, network=None, volumes={},
               **kwargs):
        """
        Create a new container.

        :param name:
            The name for the container. This will be prefixed with the
            namespace.
        :param image:
            The image tag or image object to create the container from.
        :param network:
            The network to connect the container to. The container will be
            given an alias with the ``name`` parameter. Note that, unlike the
            Docker Python client, this parameter can be a ``Network`` model
            object, and not just a network ID or name.
        :param volumes:
            A mapping of volumes to bind parameters. The keys to this mapping
            can be any of three types of objects:
            - A ``Volume`` model object
            - The name of a volume (str)
            - A path on the host to bind mount into the container (str)

            The bind parameters, i.e. the values in the mapping, can be of
            two types:
            - A full bind specifier (dict), for example
              ``{'bind': '/mnt', 'mode': 'rw'}``
            - A "short-form" bind specifier (str), for example ``/mnt:rw``
        :param fetch_image:
            Whether to attempt to pull the image if it is not found locally.
        :param kwargs:
            Other parameters to create the container with.
        """
        create_kwargs = {
            'detach': True,
        }

        # Convert network & volume models to IDs
        network = self._network_for_container(network, kwargs)
        if network is not None:
            network_id, network = (
                self._network_helper._get_id_and_model(network))
            create_kwargs['network'] = network_id

        if volumes:
            create_kwargs['volumes'] = self._volumes_for_container(volumes)

        create_kwargs.update(kwargs)

        if fetch_image:
            self._image_helper.fetch(image)

        container = super().create(name, image, **create_kwargs)

        if network is not None:
            self._connect_container_network(container, network, aliases=[name])

        return container

    def _network_for_container(self, network, create_kwargs):
        # If a network is specified use that
        if network is not None:
            return network

        # If 'network_mode' is used or networking is disabled, don't handle
        # networking.
        if (create_kwargs.get('network_mode') is not None or
                create_kwargs.get('network_disabled', False)):
            return None

        # Else, use the default network
        return self._network_helper.get_default()

    def _volumes_for_container(self, volumes):
        create_volumes = {}
        for vol, opts in volumes.items():
            try:
                vol_id, _ = self._volume_helper._get_id_and_model(vol)
            except docker.errors.NotFound:
                # Assume this is a bind if we can't find the ID
                vol_id = vol

            if vol_id in create_volumes:
                raise ValueError(
                    "Volume '{}' specified more than once".format(vol_id))

            # Short form of opts
            if isinstance(opts, str):
                opts = _parse_volume_short_form(opts)
            # Else assume long form

            create_volumes[vol_id] = opts
        return create_volumes

    def _connect_container_network(self, container, network, **connect_kwargs):
        # FIXME: Hack to make sure the container has the right network aliases.
        # Only the low-level Docker client API allows us to specify endpoint
        # aliases at container creation time:
        # https://docker-py.readthedocs.io/en/stable/api.html#docker.api.container.ContainerApiMixin.create_container
        # If we don't specify a network when the container is created then the
        # default bridge network is attached which we don't want, so we
        # reattach our custom network as that allows specifying aliases.
        network.disconnect(container)
        network.connect(container, **connect_kwargs)
        # Reload the container data to get the new network setup
        container.reload()
        # We could also reload the network data to update the containers that
        # are connected to it but that listing doesn't include containers that
        # have been created and connected but not yet started. :-/

    def status(self, container):
        container.reload()
        log.debug("Container '{}' has status '{}'".format(
            container.name, container.status))
        return container.status

    def start(self, container):
        log.info("Starting container '{}'...".format(container.name))
        container.start()
        # If the container is short-lived, it may have finished and exited
        # before we check its status.
        assert self.status(container) in ['running', 'exited']

    def stop(self, container, timeout=5):
        log.info("Stopping container '{}'...".format(container.name))
        container.stop(timeout=timeout)
        assert self.status(container) != 'running'

    def remove(self, container, force=True, volumes=True):
        """
        Remove a container.

        :param container: The container to remove.
        :param force:
            Whether to force the removal of the container, even if it is
            running. Note that this defaults to True, unlike the Docker
            default.
        :param volumes:
            Whether to remove any volumes that were created implicitly with
            this container, i.e. any volumes that were created due to
            ``VOLUME`` directives in the Dockerfile. External volumes that were
            manually created will not be removed. Note that this defaults to
            True, unlike the Docker default (where the equivalent parameter,
            ``v``, defaults to False).
        """
        super().remove(container, force=force, v=volumes)

    def stop_and_remove(self, container, stop_timeout=5, remove_force=True):
        self.stop(container, timeout=stop_timeout)
        self.remove(container, force=remove_force)

    def _teardown_remove(self, container):
        if container.status == 'running':
            self.stop(container)
        self.remove(container)


class ImageHelper:
    def __init__(self, client):
        self.images = client.images

    def fetch(self, tag):
        try:
            image = self.images.get(tag)
        except docker.errors.ImageNotFound:
            log.info("Pulling tag '{}'...".format(tag))
            image = self.images.pull(tag)

        log.debug("Found image '{}' for tag '{}'".format(image.id, tag))
        return image


class NetworksHelper(HelperBase):
    def __init__(self, client, namespace):
        super().__init__(client.networks, namespace)
        self._default_network = None

    def _teardown(self):
        # Remove the default network
        if self._default_network is not None:
            self.remove(self._default_network)
            self._default_network = None

        # Remove all other networks
        super()._teardown()

    def get_default(self, create=True):
        """
        Get the default bridge network that containers are connected to if no
        other network options are specified.

        :param create:
            Whether or not to create the network if it doesn't already exist.
        """
        if self._default_network is None and create:
            log.debug("Creating default network...")
            self._default_network = self.create('default', driver='bridge')

        return self._default_network

    def create(self, name, check_duplicate=True, **kwargs):
        """
        Create a new network.

        :param name:
            The name for the network. This will be prefixed with the namespace.
        :param check_duplicate:
            Whether or not to check for networks with the same name. Docker
            allows the creation of multiple networks with the same name (unlike
            containers). This seems to cause problems sometimes for some reason
            (?). The Docker Python client _claims_ (as of 2.5.1) that
            ``check_duplicate`` defaults to True but it actually doesn't. We
            default it to True ourselves here.
        :param kwargs:
            Other parameters to create the network with.
        """
        return super().create(name, check_duplicate=check_duplicate, **kwargs)


class VolumesHelper(HelperBase):
    def __init__(self, client, namespace):
        super().__init__(client.volumes, namespace)

    def create(self, name, **kwargs):
        """
        Create a new volume.

        :param name:
            The name for the volume. This will be prefixed with the namespace.
        :param kwargs:
            Other parameters to create the volume with.
        """
        return super().create(name, **kwargs)


class DockerHelper:
    def __init__(self, namespace='test', client=None):
        self._namespace = namespace
        if client is None:
            client = docker.client.from_env()
        self._client = client

        self.images = ImageHelper(self._client)
        self.networks = NetworksHelper(self._client, namespace)
        self.volumes = VolumesHelper(self._client, namespace)
        self.containers = ContainerHelper(
            self._client, namespace, self.images, self.networks, self.volumes)

    def teardown(self):
        self.containers._teardown()
        self.networks._teardown()
        self.volumes._teardown()

        # We need to close the underlying APIClient explicitly to avoid
        # ResourceWarnings from unclosed HTTP connections.
        self._client.api.close()
