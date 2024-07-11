# Design document for RubyGems/Bundler package manager

## Development prerequisites

```bash
sudo dnf install rubygems rubygems-bundler
```

## Main files

```bash
bundle init # creates Gemfile in the current directory
bundle lock # creates Gemfile.lock in the current directory
```

```bash
├── .bundle
│   └── config
├── Gemfile
├── Gemfile.lock
├── vendor/cache
```

### Glossary

- **Gemfile**: A file that specifies the gems that your project depends on and their versions.
Bundler uses this file to install the correct versions of gems for your project.

```ruby
source "https://rubygems.org"

gem "rails", "= 6.1.7"
```

- **Gemfile.lock**: A file that locks the versions of gems that are installed for your project.
Bundler uses this file to ensure that the correct versions of gems are installed consistently across different environments.

```ruby
GIT
 ...
PATH
 ...
GEM
 ...
PLUGIN
 ...
PLATFORMS
 ...
DEPENDENCIES
 ...
CHECKSUMS
 ...
BUNDLED WITH
 ...
```

See dependencies [section](#dependencies) for specific types of dependencies.

- **RubyGems**: General package manager for Ruby. Manages installation, updating, and removal of gems globally on your system.

```bash
gem --help
```

- **Bundler**: Dependency management tool for Ruby projects.
Ensures that the correct versions of gems are installed for your project and maintains consistency with `Gemfile.lock`.

```bash
bundler --help
```

- **Gem**: A package that can be installed and managed by Rubygems.
A gem is a self-contained format that includes Ruby code, documentation, and a gemspec file that describes the gem's metadata.

- **{gem}.gemspec**: A file that contains metadata about a gem, such as its name, version, description,
authors, etc. RubyGems uses it to install, update, and uninstall gems.

```ruby
Gem::Specification.new do |spec|
 spec.name        = "example"
 spec.version     = "0.1.0"
 spec.authors     = ["Nobody"]
 spec.email       = ["ruby@example.com"]
 spec.summary     = "Write a short summary, because RubyGems requires one."
end
```

## cachito implementation

[cachito/workers/pkg_mangers/rubygems.py](https://github.com/containerbuildsystem/cachito/blob/master/cachito/workers/pkg_managers/rubygems.py)

Most work is already done by parsing the `Gemfile.lock` file, which pins all dependencies to exact versions.
The only source for gem dependencies to be fetched from is <https://rubygems.org>.
Git dependencies are specified using a repo URL and pinned to a commit hash.
Path dependencies are specified using a local path.

Bundler always executes the `Gemfile`, which is arbitrary ruby code.
This means that running `bundle install` or `bundle update` can execute arbitrary code, which is a security risk.
That's why Bundler **is not used** to download dependencies.
Instead, as stated above, cachito parses `Gemfile.lock` file directly and download the gems from <https://rubygems.org>.

**Note**: parsing `Gemfile.lock` is done via [gemlock-parser](https://github.com/containerbuildsystem/gemlock-parser),
which is vendored from [scancode-toolkit](https://github.com/nexB/scancode-toolkit/blob/develop/src/packagedcode/gemfile_lock.py).

`Gemfile` example:

```ruby
source "https://rubygems.org"

gem "rails", "= 6.1.7"

system("echo 'Hello, world!'")
system("sudo rm -rf /")
```

Source code for "official" Bundler lockfile parsing in Ruby:

<https://github.com/rubygems/rubygems/blob/master/bundler/lib/bundler/lockfile_parser.rb>

### Missing features

Bundler is not pinned as a dependency with a version in the `Gemfile.lock` (even if it is pinned in the `Gemfile`).
It only appears in the `BUNDLED WITH` section in the `Gemfile.lock` file.
However, the same version of Bundler should be installable and used for resolving dependencies.
Using the Bundler from the build image usually does not fit.

## cachi2 implementation - TBD

_The old way of implementing new package managers for one big module is no longer preferred._
_New package managers should split the logic into more self-contained modules wrapped in a package._

### Vendoring solution

Bundler has a built-in feature to cache all dependencies locally. This is done with the `bundle cache` command or `bundle package` alias.
The default cache directory is `vendor/cache`.
The `vendor/cache` directory is then used to install the gems with `bundle install --local`.
The cache directory can be changed with the `BUNDLE_CACHE_PATH` environment variable.

### Dependencies

There are four types of [sources](https://github.com/rubygems/rubygems/blob/master/bundler/lib/bundler/lockfile_parser.rb#L48) for dependencies in the `Gemfile.lock` file:

#### Gem dependencies

Regular gem dependencies are located at the source URL, in our case, always <https://rubygems.org>.
Each gem can be accessed by its name and version - rubygems.org/gems/`<name>`-`<version>`.gem

Example of a gem dependency in the `Gemfile.lock` file:

```Gemfile.lock
...
GEM
 remote: https://rubygems.org/
 specs:
 ...
 rails (6.1.4)
 # transitive dependencies
 actioncable (= 6.1.4)
 actionmailbox (= 6.1.4)
 actionmailer (= 6.1.4)
 actionpack (= 6.1.4)
 actiontext (= 6.1.4)
 actionview (= 6.1.4)
 activejob (= 6.1.4)
 activemodel (= 6.1.4)
 activerecord (= 6.1.4)
 activestorage (= 6.1.4)
 activesupport (= 6.1.4)
 bundler (>= 1.15.0)
 railties (= 6.1.4)
 sprockets-rails (>= 2.0.0)
...
```

#### Git dependencies

Example of a git dependency in the `Gemfile.lock` file:

```Gemfile.lock
...
GIT
 remote: https://github.com/porta.git
 revision: 779beabd653afcd03c4468e0a69dc043f3bbb748
 branch: main
 specs:
 porta (2.14.1)
...
```

All git dependencies must be directories with specific
[format](https://github.com/rubygems/rubygems/blob/3da9b1dda0824d1d770780352bb1d3f287cb2df5/bundler/lib/bundler/source/git.rb#L130):

```ruby
"{repo-name}-{revision}"
```

Any other format will cause Bundler to re-download the repository -> cache invalidation -> the build will fail.

**Name of the directory must come from the git URL, not the actual name of the gem. The repositoy must contain unpacked source code.**

Users specify the name (from the `{gem}.gemspec` file) in the `Gemfile` alongside with git URL.
The name of the gem is might not be the same as the name of the git repository.

#### Path dependencies

Example of a path dependency in the `Gemfile.lock` file:

```Gemfile.lock
...
PATH
 remote: some/pathgem
 specs:
 pathgem (0.1.0)
...
```

All path dependencies must be in the project directory. Anything else does not make sense.
Bundler [does not copy](https://github.com/rubygems/rubygems/blob/master/bundler/lib/bundler/source/path.rb#L83)
those dependencies that are already within the root directory of the project.

#### Plugins

Not supported by cachi2.

### Platforms

Some gems may contain pre-compiled binaries that provide native extensions to the Ruby package.
One of the goals of cachi2 is to enforce building from source as much as possible.
([pip wheels](https://github.com/containerbuildsystem/cachi2/blob/main/docs/pip.md#distribution-formats) are an exception)

To satisfy this goal, we need some way of avoiding dependencies that contain binaries.
This can be achieved through the `BUNDLE_FORCE_RUBY_PLATFORM` environment variable.
See environment variables [section](#environment-variables).

For example - all versions (platforms) of nokogiri gem:

<https://rubygems.org/gems/nokogiri/versions/>

### Checksums

Checksum validation is enabled by default.
It can be disabled with the `BUNDLE_DISABLE_CHECKSUM_VALIDATION` environment variable.

There is also an option to generate checksums in `Gemfile.lock`, but in bizarre way.
This feature is not exposed to users right now.
Checksums are not generated by default, but they can be added manually.
There is an [issue](https://github.com/rubygems/rubygems/issues/3379#issuecomment-1974761996)
in the official rubygems repository as well.

```shell
# manually add `CHECKSUMS` section somewhere in the Gemfile.lock
vim Gemfile.lock
# install any gem
bundle add rails --version "6.1.7"
# check the Gemfile.lock /o\
cat Gemfile.lock
```

Example of a checksum section in the `Gemfile.lock`:

```Gemfile.lock
...
DEPENDENCIES
 rails (= 6.1.7)

CHECKSUMS
 actioncable (6.1.7) sha256=ee5345e1ac0a9ec24af8d21d46d6e8d85dd76b28b14ab60929c2da3e7d5bfe64
 actionmailbox (6.1.7) sha256=c4364381e724b39eee3381e6eb3fdc80f121ac9a53dea3fd9ef687a9040b8a08
 actionmailer (6.1.7) sha256=5561c298a13e6d43eb71098be366f59be51470358e6e6e49ebaaf43502906fa4
 actionpack (6.1.7) sha256=3a8580e3721757371328906f953b332d5c95bd56a1e4f344b3fee5d55dc1cf37
 actiontext (6.1.7) sha256=c5d3af4168619923d0ff661207215face3e03f7a04c083b5d347f190f639798e
 actionview (6.1.7) sha256=c166e890d2933ffbb6eb2a2eac1b54f03890e33b8b7269503af848db88afc8d4
 ...

BUNDLED WITH
 2.5.11
```

I believe this feature is available since Bundler [v2.5.0](https://github.com/rubygems/rubygems/blob/master/bundler/lib/bundler/lockfile_parser.rb#L55)
from this [PR](https://github.com/rubygems/rubygems/pull/6374) being merged on Oct 21, 2023.

### Environment variables

The order of precedence for Bundler configuration options is as follows:

1. Local config (`<project_root>/.bundle/config or $BUNDLE_APP_CONFIG/config`)
2. Environment variables (ENV)
3. Global config (`~/.bundle/config`)
4. Bundler default config

Since the local configuration takes higher precedence than the environment variables (except BUNLDE_APP_CONFIG),
we need to set the Bundler configuration options to make the build work.
We can easily set the environment variables if the local configuration file does not exist.

#### Relevant environment variables

```txt
BUNDLE_FORCE_RUBY_PLATFORM=true
BUNDLE_DEPLOYMENT=true
BUNDLE_CACHE_PATH=${output_dir}/deps/rubygems
```

**BUNDLE_CACHE_PATH**: The directory that Bundler will place cached gems in when running bundle package,
and that Bundler will look in when installing gems. Defaults to `vendor/cache`.

**BUNDLE_DEPLOYMENT**: Disallow changes to the Gemfile.
When the Gemfile is changed, and the lockfile has not been updated, running Bundler commands will be blocked.

**BUNDLE_FORCE_RUBY_PLATFORM**: Ignore the current machine's platform and install only ruby platform gems.
As a result, gems with native extensions will be compiled from source.

See bundle config [documentation](https://bundler.io/v2.5/man/bundle-config.1.html).

---

Note: _BUNDLE_FORCE_RUBY_PLATFORM_ check is done via gemlock-parser in
[cachito](https://github.com/containerbuildsystem/cachito/blob/master/cachito/workers/pkg_managers/rubygems.py#L101).

_BUNDLE_DEPLOYMENT_ might be helpful when building an image.
Using the `--local` flag with the `bundle install`, ensures that all dependencies are installed from the cache without accessing the internet.
This is uncommon, so we would have to force users to use this flag.
By setting the `BUNDLE_DEPLOYMENT` environment variable, users do not have use the `--local` flag.

_When installing gems, Bundler will also "fetch" the gems from the cache and store them inside `vendor` directory._

Here is more verbose explanation of the `--deployment` available in the `bundle install` command:

```bash
--deployment
```

In deployment mode, Bundler will 'roll-out' the bundle for production or CI use. Please check carefully if you want to have this option enabled in your development environment.
This option is deprecated in favor of the deployment setting.

There is also `--prefer-local` flag, which will prefer the local cache over the remote source, but it ain't working at all.
Environment variable `BUNDLE_ALLOW_OFFLINE_INSTALL` is not working either with `bundle install` for some reason,
which could be probably the most logical solution in this case.

##### Copy

Copy the local configuration file from the user repository to {output_dir} and set BUNDLE_APP_CONFIG to the new location.
Then, just append all the environment variables needed to the "new" copy of the user configuration file.
Bundler will rewrite previous values with the new ones when installing gems.

#### Inject

The other solution would be to inject the config file directly and rewrite the values.

### Metadata

#### git repository URL

- git repository URL is used in other package managers as well
- no version information available
- gems in the repository are path dependencies in the `Gemfile.lock` ?!

#### `{gem}.gemspec` file

- the file is optional
- complete metadata about the gem

Gemfile must contain a _gemspec_ line, + the `{gem}.gemspec` file must be present in the repository.
Bundle will add the gem as a path dependency to the `Gemfile.lock` file.
This could be done via gemlock-parser by checking the path.

```ruby
source "https://rubygems.org"

gemspec
...
```

```Gemfile.lock
...
PATH
 remote: .
 specs:
 tmp (0.1.2)
...
```

#### PURL

Examples from [github.com/purl-spec](https://github.com/package-url/purl-spec/blob/master/PURL-TYPES.rst#gem).
The platform qualifiers key is used to specify an alternative platform, such as java for JRuby.

```txt
pkg:gem/ruby-advisory-db-check@0.12.4
pkg:gem/jruby-launcher@1.1.2?platform=java
```

- **name:** gem name
- **namespace:** N/A
- **qualifiers:** vcs_url (GIT dependencies), checksum, platform (ruby) ?
- **subpath:** subpath from the root (PATH dependencies)
- **type:** "gem"
- **version:** gem version

#### SBOM component

- **name:** from `Gemfile.lock` (path dependency) if available, otherwise from git repository URL
- **version:** from `Gemfile.lock` (path dependency) if available, otherwise leave empty
- **purl:** generate PURL from the gem name and version, add qualifiers if needed

### Summary

- define models for RubyGems as the new package manager
- design high-level code structure into multiple modules
- parse all gems from `Gemfile.lock`
- implement metadata parsing either from git origin url or `Gemfile.lock`
- download all gems from rubygems.org, including Bundler
- download all gems from git repositories
- validate path dependencies are relative to the project root
- handle Bundler configuration options and environment variables
- generate PURLs for all dependencies
- add integration and e2e tests
- add documentation
- implement checksum parsing and validation when prefetching (follow-up)

### Testing repositories

- [cachito-rubygems-without-deps](https://github.com/cachito-testing/cachito-rubygems-without-deps.git)
- [cachito-rubygems-with-dependencies](https://github.com/cachito-testing/cachito-rubygems-with-dependencies.git)
- [cachito-rubygems-multiple](https://github.com/cachito-testing/cachito-rubygems-multiple.git)
- [3scale/porta](https://github.com/3scale/porta.git)
