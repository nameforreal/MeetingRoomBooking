from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Count,Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404,redirect,render
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Sum, F, ExpressionWrapper, DurationField
from datetime import timedelta, date
from .forms import RoomForm, UserEditForm
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from rooms.models import Booking,Room
from rooms import booking_operations as operations
from rooms import booking_rules as rules
from rooms.permissions import staff_permission_required, staff_has_perm, can_edit_account
from django.core.exceptions import PermissionDenied
from rooms.audit import request_context
from rooms.account_operations import save_account
from rooms.csv_export import export_bookings
from .booking_filters import filtered_bookings

@staff_permission_required("rooms.view_booking")
def overview(request):
 q=Booking.objects.select_related('room','user'); now=timezone.now()
 return render(request,'management/overview.html',{'rooms_total':Room.objects.count(),'active_rooms':Room.objects.filter(is_active=True).count(),'users_total':get_user_model().objects.count() if staff_has_perm(request.user, 'auth.view_user') else None,'bookings_total':q.count(),'pending_total':q.filter(status='pending').count(),'upcoming_total':q.filter(end_time__gte=now).exclude(status='cancelled').count(),'recent':q.order_by('-created_at')[:8],'popular':Room.objects.annotate(total=Count('bookings',filter=Q(bookings__status__in=['pending','confirmed']))).order_by('-total')[:5]})

@staff_permission_required("rooms.view_booking")
def bookings(request):
 q,s,term=filtered_bookings(request.GET)
 q=q.order_by('-start_time')
 return render(request,'management/bookings.html',{'items':q[:300],'count':q.count(),'status_filter':s,'query':term})

@staff_permission_required("rooms.change_booking")
@require_POST
@request_context("management")
def status(request,pk):
 b=get_object_or_404(Booking.objects.select_related('room'),pk=pk); target=request.POST.get('status')
 if target not in ('confirmed','cancelled') or (target=='confirmed' and b.status!='pending'):
  messages.error(request,'Недопустимое изменение статуса.'); return redirect('management:bookings')
 try:
   if target == "confirmed": operations.confirm(b, actor=request.user, revision=request.POST.get("revision"))
   else: operations.cancel(b, actor=request.user, revision=request.POST.get("revision"))
 except ValidationError as e:
  messages.error(request, "; ".join(e.messages))
 else: messages.success(request,'Статус обновлён.')
 return redirect('management:bookings')

@staff_permission_required("rooms.view_room")
def rooms(request):
 q=Room.objects.annotate(total=Count('bookings',filter=Q(bookings__status__in=['pending','confirmed']))).order_by('name'); term=request.GET.get('q','').strip()[:100]
 if term: q=q.filter(Q(name__icontains=term)|Q(description__icontains=term))
 return render(request,'management/rooms.html',{'items':q,'query':term})

@staff_permission_required("auth.view_user")
def users(request):
 U=get_user_model(); q=U.objects.annotate(total=Count('bookings')).order_by('username'); term=request.GET.get('q','').strip()[:100]
 if term: q=q.filter(Q(username__icontains=term)|Q(email__icontains=term))
 return render(request,'management/users.html',{'items':q[:300],'query':term})

@staff_permission_required("rooms.export_booking")
@require_GET
def export_csv(request):
 q,_,_=filtered_bookings(request.GET)
 return export_bookings(q)


@staff_member_required
@request_context("management")
def room_edit(request, pk=None):
    permission = "rooms.add_room" if pk is None else "rooms.change_room"
    if not staff_has_perm(request.user, permission):
        raise PermissionDenied
    room = get_object_or_404(Room, pk=pk) if pk is not None else None
    form = RoomForm(request.POST or None, request.FILES or None, instance=room)
    if request.method == "POST" and form.is_valid():
        try:
            form.save()
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Переговорная сохранена.")
            return redirect("management:rooms")
    return render(request, "management/room_form.html", {"form": form, "room": room})


@staff_permission_required("rooms.change_room")
@require_POST
@request_context("management")
def room_toggle(request, pk):
    room = get_object_or_404(Room, pk=pk)
    target = request.POST.get("is_active")
    if target not in ("true", "false"):
        return HttpResponse("Передайте целевое состояние комнаты (is_active=true/false).", status=400)
    room.is_active = target == "true"
    try:
        room.save(update_fields=["is_active"])
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
    else:
        messages.success(request, "Доступность комнаты изменена.")
    return redirect("management:rooms")


@staff_permission_required("auth.change_user")
@request_context("management")
def user_edit(request, pk):
    target = get_object_or_404(get_user_model(), pk=pk)
    if not can_edit_account(request.user, target):
        raise PermissionDenied
    form = UserEditForm(request.POST or None, instance=target, allow_roles=request.user.is_superuser)
    if request.method == "POST" and form.is_valid():
        if request.user.is_superuser and target.pk == request.user.pk and (not form.cleaned_data["is_staff"] or not form.cleaned_data["is_active"]):
            form.add_error(None, "Нельзя отключить собственный административный доступ.")
        elif request.user.is_superuser and target.is_superuser and not form.cleaned_data["is_staff"]:
            form.add_error(None, "Нельзя снять права сотрудника с суперпользователя.")
        else:
            try:
                save_account(form.instance, actor=request.user, fields=form.changed_data)
            except ValidationError as error:
                form.add_error(None, error)
            else:
                messages.success(request, "Пользователь обновлён.")
                return redirect("management:users")
    return render(request, "management/user_form.html", {"form": form, "target": target})


@staff_permission_required("rooms.view_booking")
def analytics(request):
    today = rules.site_today()
    try:
        start = rules.parse_calendar_date(request.GET.get("from"), default=today - timedelta(days=29))
        end = rules.parse_calendar_date(request.GET.get("to"), default=today)
        if end < start or (end - start).days > 365:
            raise ValidationError("Выберите период не более 366 дней с окончанием не раньше начала.")
    except ValidationError as error:
        messages.error(request, "; ".join(error.messages))
        start, end = today - timedelta(days=29), today
    qs = Booking.objects.filter(start_time__date__gte=start, start_time__date__lte=end).exclude(status="cancelled")
    by_room = list(Room.objects.annotate(usage=Count("bookings", filter=Q(bookings__start_time__date__gte=start, bookings__start_time__date__lte=end, bookings__status__in=["pending", "confirmed"]))).order_by("-usage", "name")[:12])
    total = qs.count()
    peak = max((r.usage for r in by_room), default=0)
    for room in by_room:
        room.percent = round(room.usage / peak * 100) if peak else 0
    return render(request, "management/analytics.html", {"start": start.isoformat(), "end": end.isoformat(), "total": total, "pending": qs.filter(status="pending").count(), "confirmed": qs.filter(status="confirmed").count(), "by_room": by_room})
