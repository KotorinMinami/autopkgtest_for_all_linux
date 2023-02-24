import sys

if __name__ == "__main__":
    argv = sys.argv[1:]
    full_argv = ' '.join(argv)
    autopkgtest_argv = ' -- '.join(full_argv.split(' -- ')[:-1])
    attach_argv = full_argv.split(' -- ')[-1]
    
        