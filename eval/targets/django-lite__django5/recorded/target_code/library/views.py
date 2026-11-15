import json

from django.http import JsonResponse

from . import services
from .models import Workshop


def workshop_list(request):
    data = [{"id": w.id, "title": w.title} for w in Workshop.objects.all()]
    return JsonResponse(data, safe=False)


def register_view(request, workshop_id):
    body = json.loads(request.body)
    workshop = Workshop.objects.filter(pk=workshop_id).first()
    if workshop is None:  # Q-2 (fix): was an unhandled DoesNotExist (HTTP 500)
        return JsonResponse({"error": "Workshop not found"}, status=404)
    try:
        reg = services.register(workshop, body["email"])
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"id": reg.id, "waitlisted": reg.waitlisted}, status=201)
