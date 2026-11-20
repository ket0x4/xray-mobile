project('phosh-xray-quick-setting', 'c', version: '1.0', license: 'GPL-3.0-or-later')

phosh_src = get_option('phosh_src')
if phosh_src == ''
  error('Pass -Dphosh_src=/path/to/unpacked/phosh source')
endif

plugins_dir = dependency('phosh-plugins').get_variable(pkgconfig: 'quick_setting_plugins_dir')

deps = [
  dependency('phosh-plugins'),
  dependency('gio-2.0'),
  dependency('gio-unix-2.0'),
  dependency('gtk+-3.0'),
  dependency('libhandy-1'),
]

shared_module(
  'phosh-plugin-xray-quick-setting',
  'xray-quick-setting.c',
  c_args: ['-DG_LOG_DOMAIN="phosh-plugin-xray-quick-setting"'],
  include_directories: include_directories(phosh_src / 'src'),
  dependencies: deps,
  install: true,
  install_dir: plugins_dir,
)

conf = configuration_data()
conf.set('plugins_dir', plugins_dir)
configure_file(
  input: 'xray-quick-setting.plugin.in',
  output: 'xray-quick-setting.plugin',
  configuration: conf,
  install: true,
  install_dir: plugins_dir,
)
