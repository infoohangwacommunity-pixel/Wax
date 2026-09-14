"""wax.interfaces — interface adapters into WAX.

Interfaces are adapters, NOT the runtime. (Directive §48, §49, INV-02)
Architecture: Interface → Adapter → WAX Runtime → Intelligence → ...

Each interface adapter is responsible for:
1. Receiving messages from its platform (WhatsApp webhook, HTTP request, etc.)
2. Resolving the platform-specific identifier to a WAX Principal
3. Submitting the message as an Objective to the WAX runtime
4. Returning the runtime's response in the platform-specific format

Adapters MUST NOT:
- Make authorization decisions (runtime's job)
- Bypass the runtime to call capabilities directly
- Hold business logic (e.g., 'if message contains 'tutor', do X')
"""
