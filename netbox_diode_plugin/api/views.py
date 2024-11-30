#!/usr/bin/env python
# Copyright 2024 NetBox Labs Inc
"""Diode NetBox Plugin - API Views."""

from typing import Any, Dict, Optional

from django.conf import settings
from packaging import version
from django.core.cache import cache
from django.core.exceptions import FieldError
from django.db import transaction
from django.db.models import Q
from extras.models import CachedValue
from rest_framework import status, views
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from utilities.api import get_serializer_for_model

from netbox_diode_plugin.api.permissions import IsDiodeReader, IsDiodeWriter
from netbox_diode_plugin.api.serializers import (
    ApplyChangeSetRequestSerializer,
    ObjectStateSerializer,
)

# Determine NetBoxType based on version
if version.parse(settings.VERSION).major >= 4:
    from core.models import ObjectType as NetBoxType
else:
    from django.contrib.contenttypes.models import ContentType as NetBoxType


class ObjectStateView(views.APIView):
    """ObjectState view."""

    permission_classes = [IsAuthenticated, IsDiodeReader]

    def _get_lookups(self, object_type_model: str) -> tuple:
        """Return related object lookups, cached for performance."""
        cache_key = f"lookups_{object_type_model.lower()}"
        lookups = cache.get(cache_key)

        if not lookups:
            if "'ipam.models.ip.ipaddress'" in object_type_model:
                lookups = (
                    "assigned_object",
                    "assigned_object__device",
                    "assigned_object__device__site",
                )
            elif "'dcim.models.device_components.interface'" in object_type_model:
                lookups = "device", "device__site"
            elif "'dcim.models.devices.device'" in object_type_model:
                lookups = ("site",)
            else:
                lookups = ()

            cache.set(cache_key, lookups, timeout=3600)  # Cache for 1 hour

        return lookups

    def get(self, request, *args, **kwargs):
        """Return object state."""
        object_type = request.query_params.get("object_type")
        if not object_type:
            raise ValidationError("object_type parameter is required")

        app_label, model_name = object_type.split(".")
        cache_key = f"object_model_{app_label}_{model_name}"
        object_content_type = cache.get(cache_key)

        if not object_content_type:
            object_content_type = NetBoxType.objects.get_by_natural_key(
                app_label, model_name
            )
            cache.set(cache_key, object_content_type, timeout=3600)

        object_type_model = object_content_type.model_class()
        object_id = request.query_params.get("id")

        if object_id:
            queryset = object_type_model.objects.filter(id=object_id)
        else:
            search_value = request.query_params.get("q")
            if not search_value:
                raise ValidationError("id or q parameter is required")

            query_filter = Q(value__iexact=search_value, object_type=object_content_type)
            cached_values = CachedValue.objects.filter(query_filter).values_list(
                "object_id", flat=True
            )

            queryset = object_type_model.objects.filter(id__in=cached_values)
            lookups = self._get_lookups(str(object_type_model).lower())
            if lookups:
                queryset = queryset.prefetch_related(*lookups)

        serializer = ObjectStateSerializer(
            queryset,
            many=True,
            context={"request": request, "object_type": object_type},
        )

        return Response(serializer.data[0] if serializer.data else {})


class ApplyChangeSetView(views.APIView):
    """ApplyChangeSet view."""

    permission_classes = [IsAuthenticated, IsDiodeWriter]

    @staticmethod
    def _get_object_type_model(object_type: str):
        """Cached lookup for object type model."""
        app_label, model_name = object_type.split(".")
        cache_key = f"object_model_{app_label}_{model_name}"
        model_class = cache.get(cache_key)

        if not model_class:
            object_content_type = NetBoxType.objects.get_by_natural_key(
                app_label, model_name
            )
            model_class = object_content_type.model_class()
            cache.set(cache_key, model_class, timeout=3600)

        return model_class

    def post(self, request, *args, **kwargs):
        """Apply change sets with caching and batch operations."""
        serializer = ApplyChangeSetRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        change_set = serializer.validated_data["change_set"]
        grouped_changes = {}
        for change in change_set:
            grouped_changes.setdefault(change["object_type"], []).append(change)

        serializer_errors = []

        try:
            with transaction.atomic():
                for object_type, changes in grouped_changes.items():
                    model_class = self._get_object_type_model(object_type)
                    change_data = []

                    for change in changes:
                        change_type = change["change_type"]
                        object_data = change["data"]
                        object_id = change.get("object_id")

                        if change_type == "create":
                            change_data.append(model_class(**object_data))
                        elif change_type == "update":
                            instance = model_class.objects.get(id=object_id)
                            for attr, value in object_data.items():
                                setattr(instance, attr, value)
                            change_data.append(instance)
                        else:
                            serializer_errors.append(
                                {"error": f"Invalid change_type: {change_type}"}
                            )

                    if change_type == "create":
                        model_class.objects.bulk_create(change_data)
                    elif change_type == "update":
                        model_class.objects.bulk_update(
                            change_data, fields=[f.name for f in model_class._meta.fields]
                        )

                if serializer_errors:
                    raise Exception("Errors occurred in change set processing")
        except Exception as e:
            return Response({"errors": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"result": "success"}, status=status.HTTP_200_OK)
