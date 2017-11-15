import functools

from docker import models

from seaworthy.helpers import DockerHelper
from seaworthy.logs import (
    RegexMatcher, UnorderedLinesMatcher, stream_logs, wait_for_logs_matching)


def deep_merge(*dicts):
    result = {}
    for d in dicts:
        if not isinstance(d, dict):
            raise Exception('Can only deep_merge dicts, got {}'.format(d))
        for k, v in d.items():
            # Whenever the value is a dict, we deep_merge it. This ensures that
            # (a) we only ever merge dicts with dicts and (b) we always get a
            # deep(ish) copy of the dicts and are thus safe from accidental
            # mutations to shared state.
            if isinstance(v, dict):
                v = deep_merge(result.get(k, {}), v)
            result[k] = v
    return result


class _DefinitionBase:
    __model_type__ = None

    def __init__(self, name, create_kwargs=None, helper=None):
        self.name = name

        self._create_args = ()
        self._create_kwargs = {} if create_kwargs is None else create_kwargs

        self._helper = None
        self.set_helper(helper)

        self._inner = None

    def create(self, helper=None, **kwargs):
        self.set_helper(helper)
        if self.created:
            raise RuntimeError(
                '{} already created.'.format(self.__model_type__.__name__))

        kwargs = self.merge_kwargs(self._create_kwargs, kwargs)

        self._inner = self.helper.create(
            self.name, *self._create_args, **kwargs)

    def remove(self, **kwargs):
        self.helper.remove(self.inner(), **kwargs)
        self._inner = None

    def __enter__(self):
        self.create()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._teardown()

    def _teardown(self):
        if self.created:
            self.remove()

    @property
    def helper(self):
        if self._helper is None:
            raise RuntimeError('No helper set.')
        return self._helper

    def set_helper(self, helper):
        # We don't want to "unset" in this method.
        if helper is None:
            return

        # Get the right kind of helper if given a DockerHelper
        if isinstance(helper, DockerHelper):
            helper = helper._helper_for_model(self.__model_type__)

        # We already have this one.
        if helper is self._helper:
            return
        if self._helper is None:
            self._helper = helper
        else:
            raise RuntimeError('Cannot replace existing helper.')

    def as_fixture(self, name=None):
        """
        A decorator to inject this container into a function as a test fixture.
        """
        if name is None:
            name = self.name

        def deco(f):
            @functools.wraps(f)
            def wrapper(*args, **kw):
                with self:
                    kw[name] = self
                    return f(*args, **kw)
            return wrapper
        return deco

    def inner(self):
        """
        :returns: the underlying Docker model object
        """
        if not self.created:
            raise RuntimeError(
                '{} not created yet.'.format(self.__model_type__.__name__))
        return self._inner

    @property
    def created(self):
        return self._inner is not None

    def base_kwargs(self):
        """
        Override this method to provide dynamically generated base kwargs for
        the resource.
        """
        return {}

    def merge_kwargs(self, default_kwargs, kwargs):
        """
        Override this method to merge kwargs differently.
        """
        return deep_merge(self.base_kwargs(), default_kwargs, kwargs)


class ContainerDefinition(_DefinitionBase):
    """
    This is the base class for container definitions. Instances (and instances
    of subclasses) are intended to be used both as test fixtures and as
    convenient objects for operating on containers being tested.

    TODO: Document this properly.

    A container object may be used as a context manager to ensure proper setup
    and teardown of the container around the code that uses it::

        with ContainerDefinition('my_container', IMAGE, helper=ch) as c:
            assert c.status() == 'running'

    (Note that this only works if the container has a helper set and does not
    have a container created.)
    """

    __model_type__ = models.containers.Container
    WAIT_TIMEOUT = 10.0

    def __init__(self, name, image, wait_patterns=None, wait_timeout=None,
                 create_kwargs=None, helper=None):
        """
        :param name:
            The name for the container. The actual name of the container is
            namespaced by ContainerHelper. This name will be used as a network
            alias for the container.
        :param image: image tag to use
        :param list wait_patterns:
            Regex patterns to use when checking that the container has started
            successfully.
        :param wait_timeout:
            Number of seconds to wait for the ``wait_patterns``. Defaults to
            ``self.WAIT_TIMEOUT``.
        :param dict create_kwargs:
            Other kwargs to use when creating the container.
        :param seaworthy.helper.ContainerHelper helper:
            A ContainerHelper instance used to create containers.
        """
        super().__init__(name, create_kwargs=create_kwargs, helper=helper)

        self._create_args = (image,)
        if wait_patterns:
            self.wait_matchers = [RegexMatcher(p) for p in wait_patterns]
        else:
            self.wait_matchers = None
        if wait_timeout is not None:
            self.wait_timeout = wait_timeout
        else:
            self.wait_timeout = self.WAIT_TIMEOUT

        self._http_clients = []

    def __enter__(self):
        self.create_and_start()
        return self

    def _teardown(self):
        """
        Stop and remove the container if it exists.
        """
        while self._http_clients:
            self._http_clients.pop().close()
        if self._inner is not None:
            self.stop_and_remove()

    def status(self):
        """
        Get the container's current status from docker.

        If the container does not exist (before creation and after removal),
        the status is ``None``.
        """
        if not self.created:
            return None
        self.inner().reload()
        return self.inner().status

    def create_and_start(self, helper=None, fetch_image=True, **kwargs):
        """
        Create the container and start it, waiting for the expected log lines.

        :param fetch_image:
            Whether to try pull the image if it's not found. The behaviour here
            is similar to ``docker run`` and this parameter defaults to
            ``True``.
        """
        self.create(fetch_image=fetch_image, helper=helper, **kwargs)

        self.helper.start(self._inner)

        self.wait_for_start()

    def wait_for_start(self):
        """
        Wait for the container to start.

        By default this will wait for the log lines matching the patterns
        passed in the ``wait_patterns`` parameter of the constructor using an
        UnorderedLinesMatcher. For more advanced checks for container startup,
        this method should be overridden.
        """
        if self.wait_matchers:
            matcher = UnorderedLinesMatcher(*self.wait_matchers)
            self.wait_for_logs_matching(matcher, timeout=self.wait_timeout)

    def stop_and_remove(self):
        """ Stop the container and remove it. """
        self.helper.stop_and_remove(self.inner())
        self._inner = None

    def clean(self):
        """
        This method should "clean" the container so that it is in the same
        state as it was when it was started.
        """
        raise NotImplementedError()

    @property
    def ports(self):
        """
        The ports (exposed and published) of the container.
        """
        return self.inner().attrs['NetworkSettings']['Ports']

    def _host_port(self, port_spec, index):
        if port_spec not in self.ports:
            raise ValueError("Port '{}' is not exposed".format(port_spec))

        mappings = self.ports[port_spec]
        if mappings is None:
            raise ValueError(
                "Port '{}' is not published to the host".format(port_spec))

        mapping = mappings[index]
        return mapping['HostIp'], mapping['HostPort']

    def get_host_port(self, container_port, proto='tcp', index=0):
        """
        :param container_port: The container port.
        :param proto: The protocol ('tcp' or 'udp').
        :param index: The index of the mapping entry to return.
        :returns: A tuple of the interface IP and port on the host.
        """
        port_spec = '{}/{}'.format(container_port, proto)
        return self._host_port(port_spec, index)

    def get_first_host_port(self):
        """
        Get the first mapping of the first (lowest) container port that has a
        mapping. Useful when a container publishes only one port.

        Note that unlike the Docker API, which sorts ports lexicographically
        (e.g. ``90/tcp`` > ``8000/tcp``), we sort ports numerically so that the
        lowest port is always chosen.
        """
        mapped_ports = {p: m for p, m in self.ports.items() if m is not None}
        if not mapped_ports:
            raise RuntimeError('Container has no published ports')

        def sort_key(port_string):
            port, proto = port_string.split('/', 1)
            return int(port), proto
        firt_port_spec = sorted(mapped_ports.keys(), key=sort_key)[0]

        return self._host_port(firt_port_spec, 0)

    def get_logs(self, stdout=True, stderr=True, timestamps=False, tail='all',
                 since=None):
        """
        Get container logs.

        This method does not support streaming, use :meth:`stream_logs` for
        that.
        """
        return self.inner().logs(
            stdout=stdout, stderr=stderr, timestamps=timestamps, tail=tail,
            since=since)

    def stream_logs(self, stdout=True, stderr=True, tail='all', timeout=10.0):
        """
        Stream container output.
        """
        return stream_logs(
            self.inner(), stdout=stdout, stderr=stderr, tail=tail,
            timeout=timeout)

    def wait_for_logs_matching(self, matcher, timeout=10, encoding='utf-8',
                               **logs_kwargs):
        """
        Wait for logs matching the given matcher.
        """
        wait_for_logs_matching(
            self.inner(), matcher, timeout=timeout, encoding=encoding,
            **logs_kwargs)

    def http_client(self, port=None):
        """
        Construct an HTTP client for this container.
        """
        # Local import to avoid potential circularity.
        from seaworthy.client import ContainerHttpClient
        client = ContainerHttpClient.for_container(self, container_port=port)
        self._http_clients.append(client)
        return client


class NetworkDefinition(_DefinitionBase):
    __model_type__ = models.networks.Network


class VolumeDefinition(_DefinitionBase):
    __model_type__ = models.volumes.Volume