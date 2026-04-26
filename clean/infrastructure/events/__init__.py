"""
Event Bus Publisher Adapter — adapta nosso EventBus para a porta EventPublisher.
"""
from clean.application.interfaces import EventPublisher
from clean.domain.events import DomainEvent


class EventBusPublisherAdapter(EventPublisher):
    """Adapta EventBus (infra) para EventPublisher (application port)."""
    
    def __init__(self, event_bus):
        self._bus = event_bus
    
    def publish(self, event: DomainEvent) -> None:
        self._bus.publish(event.event_type, event.payload, source=event.source)
    
    def subscribe(self, event_type: str, handler) -> None:
        # Wrap handler para receber evento do bus
        def wrapped(event):
            domain_event = DomainEvent(
                event_type=event.type,
                payload=event.payload,
                timestamp=int(event.timestamp.timestamp()),
                source=event.source
            )
            handler(domain_event)
        self._bus.subscribe(event_type, wrapped)
