%{!?__python2: %define __python2 python2}
%{!?python2_sitelib: %global python2_sitelib %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print get_python_lib()")}
%{!?python2_sitearch: %global python2_sitearch %(%{__python2} -c "from distutils.sysconfig import get_python_lib; print get_python_lib(1)")}


Name:           pygtkglext
Version:        1.1.0
Release:        27.xpra4%{?dist}
Summary:        Python bindings for GtkGLExt
License:        LGPLv2+
Group:          System Environment/Libraries
URL:            http://www.k-3d.org/gtkglext/Main_Page
Source:         http://downloads.sourceforge.net/gtkglext/%{name}-%{version}.tar.bz2
BuildRoot:      %{_tmppath}/pygtkglext-%{version}-%{release}-root-%(%{__id_u} -n)
BuildRequires:  gtkglext-devel
BuildRequires:  python2-devel
BuildRequires:  pygtk2-devel
Requires:       pygtk2

%if 0%{?fedora}
Requires:       python2-pyopengl
%else
Requires:       PyOpenGL
%endif

%description
Python bindings for GtkGLExt.


%package        devel
Summary:        Development files for %{name}
Group:          Development/Libraries
Requires:       %{name} = %{version}-%{release}
Requires:       pkgconfig
Requires:       pygtk2-devel

%description    devel
The %{name}-devel package contains libraries and header files for
developing applications that use %{name}.


%prep
%setup -q
iconv -f EUC-JP -t UTF8 AUTHORS > tmp
mv tmp AUTHORS
iconv -f EUC-JP -t UTF8 README > tmp
mv tmp README


%build
%configure
make %{?_smp_mflags}


%install
rm -rf $RPM_BUILD_ROOT
make install DESTDIR=$RPM_BUILD_ROOT INSTALL="%{__install} -p"
if [ %{python2_sitelib} != %{python2_sitearch} ]; then
  mv $RPM_BUILD_ROOT%{python2_sitelib}/gtk-2.0/gtk/gdkgl/* \
     $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gdkgl
  mv $RPM_BUILD_ROOT%{python2_sitelib}/gtk-2.0/gtk/gtkgl/* \
     $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gtkgl
fi
rm $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gdkgl/_gdkgl.la
rm $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gtkgl/_gtkgl.la

# this can be executed to run some basic tests (it has a main and shebang)
sed -i "1s+.*+#!/usr/bin/python2+" $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gtkgl/apputils.py
chmod +x $RPM_BUILD_ROOT%{python2_sitearch}/gtk-2.0/gtk/gtkgl/apputils.py

# for %%doc
rm examples/Makefile*


%clean
rm -rf $RPM_BUILD_ROOT


%files
%defattr(-,root,root,-)
%doc AUTHORS ChangeLog COPYING COPYING.LIB README examples
%{python2_sitearch}/gtk-2.0/gtk/gdkgl
%{python2_sitearch}/gtk-2.0/gtk/gtkgl

%files devel
%defattr(-,root,root,-)
%{_libdir}/pkgconfig/*.pc
%{_datadir}/pygtk/2.0/defs/*


%changelog
* Tue Oct 08 2018 Antoine Martin <antoine@xpra.org> - 1.1.0-27.xpra4
- remove opensuse support, fix shebang

* Mon Sep 17 2018 Antoine Martin <antoine@xpra.org> - 1.1.0-27.xpra1
- bump revision so we upgrade the upstream package, which has broken dependencies

* Mon Sep 17 2018 Antoine Martin <antoine@xpra.org> - 1.1.0-16.xpra3
- use the package name for python2-pyopengl on Fedora

* Tue Jul 03 2018 Antoine Martin <antoine@xpra.org> - 1.1.0-16.xpra2
- use python2 explicitly

* Thu Dec 03 2015 Antoine Martin <antoine@xpra.org> - 1.1.0-16.xpra1
- Added support for building on openSUSE
