from django.urls import path

from .views import PVEAllocationRequestView, PVEAllocationStatusView

urlpatterns = [
    path(
        "allocation/<int:pk>/vm-status/",
        PVEAllocationStatusView.as_view(),
        name="pve-provisioner-allocation-status",
    ),
    path(
        "allocation/project/<int:project_pk>/create",
        PVEAllocationRequestView.as_view(),
        name="allocation-create",
    ),
]
